"""M5 부분 응답 보존 (AADS-LLM-M5-PARTIAL-PRESERVE-20260929).

1) 빈 assistant 스윕이 "50자 미만" 만으로 짧은 정상 응답·부분 응답을 숨기던 결함
2) Claude 경로 폴백 단계(원 호출 실패/대체 시도/대체 성공/전체 실패) 구분
3) 폴백 체인이 같은 후보를 다시 부르지 않음
"""
from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import chat_service


class _AcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AcquireCtx(self.conn)


def _sweep_sql() -> str:
    return " ".join(chat_service._STALE_EMPTY_ASSISTANT_SWEEP_SQL.split())


def _where(sql: str) -> str:
    return sql.split("WHERE m.role", 1)[1].split("RETURNING", 1)[0]


# ① 짧지만 내용 있는 완료 응답은 숨기지 않는다.
def test_short_completed_answer_is_not_hidden_by_length_alone():
    where = _where(_sweep_sql())
    # 본문이 trim 후 정확히 0자여야만 대상이다. "Pong! ..."(31자) 는 빠진다.
    assert "length(trim(COALESCE(m.content, ''))) = 0" in where
    # `< 50` 은 인덱스(179) 사용을 위해 남지만 단독 조건이 아니다 — `= 0` 과 AND.
    assert "length(trim(COALESCE(m.content, ''))) < 50 AND length(trim(COALESCE(m.content, ''))) = 0" in where
    assert " OR length(trim" not in where


# ② 진짜 빈 placeholder 는 정리된다 — 스윕이 실제로 돌고 결과가 집계된다.
@pytest.mark.asyncio
async def test_truly_empty_finished_row_is_swept():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="UPDATE 0")

    async def _fetchval(sql, *args):
        if sql is chat_service._STALE_EMPTY_ASSISTANT_SWEEP_SQL:
            return 1
        return 0

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    with (
        patch("app.services.chat_service._is_local_active_api_slot", return_value=True),
        patch("app.services.chat_service._live_session_ids_with_db", AsyncMock(return_value=set())),
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
    ):
        result = await chat_service.cleanup_stale_streaming_placeholders(timeout_sec=600)

    assert result["stale_empty_hidden"] == 1
    sql = _sweep_sql()
    assert "SET is_hidden = TRUE, intent = 'stale_empty_placeholder'" in sql
    assert "m.created_at < NOW() - INTERVAL '10 minutes'" in sql


# ③ 부분 응답 보존 행 — 실행 중이거나 도구 결과·산출물이 남은 행은 10분 뒤에도 살아 있다.
def test_preserved_partial_rows_survive_the_sweep():
    where = _where(_sweep_sql())
    # 실행이 아직 끝나지 않았으면 대상이 아니다.
    assert "NOT EXISTS ( SELECT 1 FROM chat_turn_executions te_live" in where
    assert "te_live.id = m.execution_id" in where
    assert "te_live.status IN ('running', 'retrying')" in where
    # 보존할 도구 결과·첨부·산출물·사고 요약이 있으면 대상이 아니다.
    assert "jsonb_array_length(m.tools_called) = 0" in where
    assert "jsonb_array_length(m.attachments) = 0" in where
    assert "m.artifact_id IS NULL" in where
    assert "length(trim(COALESCE(m.thinking_summary, ''))) = 0" in where
    # streaming_placeholder 는 별도 경로가 부분 응답으로 승격한다.
    assert "m.intent IS DISTINCT FROM 'streaming_placeholder'" in where


def test_restore_migration_only_unhides_rows_with_content():
    from pathlib import Path

    root = Path(chat_service.__file__).resolve().parents[2]
    body = (root / "migrations/20260929_stale_empty_placeholder_restore.sql").read_text(encoding="utf-8")
    code = " ".join(line for line in body.splitlines() if not line.lstrip().startswith("--"))
    code = " ".join(code.split())
    assert "SET is_hidden = FALSE" in code
    assert "intent = 'stale_empty_placeholder'" in code
    assert "length(trim(COALESCE(content, ''))) > 0" in code
    assert "DELETE FROM" not in code.upper()


# ④ 폴백 단계 4종이 구분돼 기록된다 — 화면 이벤트와 DB trace 양쪽.
def test_fallback_stages_are_distinct_in_trace_and_events():
    trace: list = []
    events = [
        chat_service._fallback_stage_event(trace, "primary_failed", from_model="claude-opus-5", reason="한도 초과"),
        chat_service._fallback_stage_event(trace, "fallback_attempt", from_model="claude-opus-5", to_model="claude-fable-5-1"),
        chat_service._fallback_stage_event(trace, "fallback_succeeded", from_model="claude-opus-5", to_model="claude-fable-5-1"),
        chat_service._fallback_stage_event(trace, "all_failed", from_model="claude-opus-5", reason="시간 초과"),
    ]
    assert [t["stage"] for t in trace] == ["primary_failed", "fallback_attempt", "fallback_succeeded", "all_failed"]
    assert [e["stage"] for e in events] == [t["stage"] for t in trace]
    assert all(e["type"] == "model_fallback" for e in events)
    labels = [e["stage_label"] for e in events]
    assert labels == ["원 호출 실패", "대체 시도", "대체 성공", "전체 실패"]
    assert len({e["content"] for e in events}) == 4
    json.dumps(trace, ensure_ascii=False)  # DB jsonb 로 그대로 들어간다


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Error 429 rate_limit_error token=sk-ant-oat01-SECRET", "한도 초과"),
        ("weekly limit · resets Sep 16", "한도 초과"),
        ("overloaded_error", "과부하"),
        ("401 unauthorized Bearer sk-ant-oat01-SECRET", "인증 오류"),
        ("ReadTimeout: timed out host=10.0.0.5:8199", "시간 초과"),
        ("connection reset by peer 10.0.0.5", "연결 오류"),
        ("retry stream ended without done event", "빈 응답"),
        ("HTTP 502 from upstream", "HTTP 502"),
        ("unexpected regenerate failure", "요청 오류"),
    ],
)
def test_fallback_reason_is_safe_category_only(raw, expected):
    reason = chat_service._safe_fallback_reason(RuntimeError(raw))
    assert reason == expected
    assert "sk-ant" not in reason and "10.0.0.5" not in reason
    event = chat_service._fallback_stage_event([], "primary_failed", from_model="m", reason=reason)
    assert "SECRET" not in json.dumps(event, ensure_ascii=False)


# ⑤ 동일 후보 재시도 차단.
def test_fallback_candidates_skip_original_tried_and_duplicates():
    tried = ["gpt-5.6-sol"]
    out = chat_service._fallback_candidates_once(
        "claude-opus-5",
        ["Claude-Opus-5", "claude-fable-5-1", "gpt-5.6-sol", "claude-fable-5-1 ", ""],
        tried,
    )
    assert out == ["claude-fable-5-1"]
    # 이미 둘 다 시도했으면 남은 후보가 없다 — 새 예산을 만들지 않는다.
    assert chat_service._fallback_candidates_once(
        "claude-opus-5", ["claude-fable-5-1", "gpt-5.6-sol"], ["claude-fable-5-1", "gpt-5.6-sol"],
    ) == []


def test_429_fallback_path_uses_shared_budget_and_records_all_stages():
    src = inspect.getsource(chat_service)
    block = src.split("_FALLBACK_CHAIN_429 = {", 1)[1].split("rate_limit_placeholder_preserved", 1)[0]
    # 실행이 공유하는 시도 목록 — 재개 경로가 새 예산을 만들지 않는다.
    assert 'state.setdefault("fallback_tried_models", [])' in block
    assert 'state.setdefault("fallback_trace", [])' in block
    assert "_fallback_candidates_once(" in block
    for stage in ("primary_failed", "fallback_attempt", "fallback_succeeded", "all_failed"):
        assert f'"{stage}"' in block
    # 전체 실패도 DB 이력에 남긴다.
    assert "_record_fallback_trace(" in block
    # 대체 실패 원문을 로그·화면으로 흘리지 않는다.
    assert "{_fb_err}" not in block


@pytest.mark.asyncio
async def test_record_fallback_trace_writes_execution_history():
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    trace: list = []
    chat_service._fallback_stage_event(trace, "primary_failed", from_model="claude-opus-5", reason="한도 초과")
    chat_service._fallback_stage_event(trace, "all_failed", from_model="claude-opus-5", reason="한도 초과")
    execution_id = "3f0c7a0e-8f6f-4c83-9f56-0d8b0a6d7c11"
    with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)):
        await chat_service._record_fallback_trace(execution_id, trace)

    sql, payload, _eid = conn.execute.await_args.args
    assert "UPDATE chat_turn_executions SET fallback_chain = $1::jsonb" in sql
    data = json.loads(payload)
    assert data["result"] == "all_failed"
    assert [s["stage"] for s in data["stages"]] == ["primary_failed", "all_failed"]
