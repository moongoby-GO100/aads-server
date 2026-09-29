"""superseded 턴이 도구만 돌고 본문이 없을 때 작업 요약을 남기는지 (2026-09-29).

24시간 superseded 무본문 37건이 전부 "보존된 내용이 없습니다" 만 남겼고, 그중
다수가 100~270초 동안 도구를 28회씩 돌린 턴이었다.
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import chat_service as svc

_PROGRESS_28 = "⏳ _AI가 응답을 생성 중입니다... (도구 28회 호출 중, 최근: run_remote_command)_"
_PROGRESS_0 = "⏳ _AI가 응답을 생성 중입니다... (도구 0회 호출 중)_"
_LEGACY_EMPTY = "보존된 내용이 없습니다"


def test_parse_progress_tool_info_reads_count_and_last_tool():
    assert svc._parse_progress_tool_info(_PROGRESS_28) == (28, "run_remote_command")
    assert svc._parse_progress_tool_info("본문\n\n⏳ _생성 중... (도구 3회 호출)_") == (3, "")
    assert svc._parse_progress_tool_info("") == (0, "")


def test_superseded_placeholder_with_tools_keeps_work_summary():
    content = svc._format_stale_placeholder_content(_PROGRESS_28, superseded=True)
    assert _LEGACY_EMPTY not in content
    assert "새 지시로 이 턴이 대체되었습니다" in content
    assert "도구 28회" in content
    assert "run_remote_command" in content
    assert "생성된 본문은 없습니다" in content


@pytest.mark.parametrize("raw", [_PROGRESS_0, "⏳ _AI가 응답을 생성 중입니다..._", ""])
def test_superseded_placeholder_without_tools_keeps_legacy_notice(raw):
    content = svc._format_stale_placeholder_content(raw, superseded=True)
    assert _LEGACY_EMPTY in content
    assert "새 지시로 이 턴이 대체" not in content


def test_non_superseded_stale_placeholder_is_unchanged():
    # 다른 중단 경로(재시작·타임아웃)는 "새 지시로 대체" 가 사실이 아니므로 기존 문구.
    assert _LEGACY_EMPTY in svc._format_stale_placeholder_content(_PROGRESS_28)


def test_tool_work_reason_still_classified_as_superseded():
    assert svc._classify_interruption_reason("superseded_tool_work_without_partial") == (
        svc._classify_interruption_reason("superseded_without_partial")
    )


def _conn(eid):
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "status": "running", "owner_instance": svc._EXECUTION_OWNER_INSTANCE,
        "owner_epoch": 2, "lease_valid": True, "completed_at": None,
    }
    started = datetime.now(timezone.utc) - timedelta(seconds=130)

    def _fetchval(query, *args):
        if "RETURNING id" in query:
            return eid
        if "SELECT started_at" in query:
            return started
        return None

    conn.fetchval.side_effect = _fetchval
    return conn


def _execution_diagnostics(conn):
    call = next(
        c for c in conn.execute.await_args_list
        if "UPDATE chat_turn_executions" in c.args[0] and "interruption_diagnostics" in c.args[0]
    )
    return json.loads(call.args[4])


@pytest.mark.asyncio
async def test_mark_superseded_with_tool_work_writes_summary_instead_of_delete(monkeypatch):
    sid, eid, pid = uuid4(), uuid4(), uuid4()
    conn = _conn(eid)
    monkeypatch.setattr(svc, "_schedule_interrupted_auto_resume", AsyncMock(return_value=False))

    await svc._mark_execution_interrupted(
        conn, str(sid), str(eid), "superseded_tool_work_without_partial",
        partial_content="", placeholder_id=str(pid), delete_empty_placeholder=False,
        tool_count=28, last_tool="run_remote_command",
    )

    queries = [c.args[0] for c in conn.execute.await_args_list]
    assert not any("DELETE FROM chat_messages" in q for q in queries)
    summary_update = next(
        c for c in conn.execute.await_args_list
        if "UPDATE chat_messages" in c.args[0] and "SET content = $1" in c.args[0]
    )
    assert "도구 28회" in summary_update.args[1]
    assert "run_remote_command" in summary_update.args[1]
    assert _LEGACY_EMPTY not in summary_update.args[1]
    assert summary_update.args[2] == pid

    diag = _execution_diagnostics(conn)
    assert diag["tool_count"] == 28
    assert diag["last_tool"] == "run_remote_command"
    assert diag["elapsed_sec"] >= 129
    assert diag["partial_len"] == 0


@pytest.mark.asyncio
async def test_mark_superseded_without_tools_keeps_existing_delete(monkeypatch):
    sid, eid, pid = uuid4(), uuid4(), uuid4()
    conn = _conn(eid)
    monkeypatch.setattr(svc, "_schedule_interrupted_auto_resume", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_streaming_state", {})

    await svc._mark_execution_interrupted(
        conn, str(sid), str(eid), "superseded_without_partial",
        partial_content="", placeholder_id=str(pid), delete_empty_placeholder=True,
    )

    queries = [c.args[0] for c in conn.execute.await_args_list]
    assert any("DELETE FROM chat_messages" in q for q in queries)
    assert not any(
        "SET content = $1" in c.args[0] and "새 지시로 이 턴이 대체" in str(c.args[1])
        for c in conn.execute.await_args_list
    )
    diag = _execution_diagnostics(conn)
    assert diag["tool_count"] == 0
    assert diag["last_tool"] == ""
    assert "elapsed_sec" in diag


@pytest.mark.asyncio
async def test_mark_superseded_reads_tool_count_from_streaming_state(monkeypatch):
    sid, eid, pid = uuid4(), uuid4(), uuid4()
    conn = _conn(eid)
    monkeypatch.setattr(svc, "_schedule_interrupted_auto_resume", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_streaming_state", {
        str(sid): {"execution_id": str(eid), "tool_count": 5, "last_tool": "read_remote_file"},
    })

    await svc._mark_execution_interrupted(
        conn, str(sid), str(eid), "superseded by newer execution",
        partial_content="", placeholder_id=str(pid),
    )

    diag = _execution_diagnostics(conn)
    assert diag["tool_count"] == 5
    assert diag["last_tool"] == "read_remote_file"
    assert not any("DELETE FROM chat_messages" in c.args[0] for c in conn.execute.await_args_list)
