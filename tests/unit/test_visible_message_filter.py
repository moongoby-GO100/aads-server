"""_visible_message_filter 저장=렌더 불일치 회귀 (AADS-CHAT-M12-RENDER-MISMATCH-P0).

필터 SQL 을 실제 PostgreSQL 임시 테이블에 적용해 판정한다. 문자열 포함 검사로는
"어떤 행이 통과하는가" 를 보장하지 못한다.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

from app.services import chat_service

asyncpg = pytest.importorskip("asyncpg")

NOTICE_58 = (
    "⚠️ _응답 생성이 중단되어 여기까지 보존된 내용이 없습니다. "
    "같은 질문으로 다시 요청할 수 있습니다._"
)
BODY_96 = (
    "이전 보고는 sudo -n 실패를 권한 부족으로 단정한 점이 잘못됐습니다. 현재 SSH 연결 상태를 재조회해 다시 보고합니다."
)
TAIL = "\n\n_(여기까지 생성한 뒤 중단되었습니다. 이어서 진행하려면 다시 요청해 주세요.)_"


async def _connect():
    if not os.getenv("PGHOST"):
        pytest.skip("PG 환경변수 없음")
    try:
        return await asyncpg.connect(
            host=os.environ["PGHOST"],
            port=int(os.getenv("PGPORT", "5432")),
            user=os.getenv("PGUSER"),
            password=os.getenv("PGPASSWORD"),
            database=os.getenv("PGDATABASE"),
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PG 접속 불가: {exc}")


async def _visible(rows, *, is_active=False, include_streaming=True):
    conn = await _connect()
    try:
        async with conn.transaction():
            await conn.execute(
                "CREATE TEMP TABLE chat_messages ("
                "id uuid, role text, intent text, is_hidden boolean, content text, quality_details jsonb"
                ") ON COMMIT DROP"
            )
            ids = {}
            for name, role, intent, hidden, content, details in rows:
                rid = uuid.uuid4()
                ids[rid] = name
                await conn.execute(
                    "INSERT INTO pg_temp.chat_messages VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
                    rid, role, intent, hidden, content, json.dumps(details) if details else None,
                )
            flt = chat_service._visible_message_filter(is_active, include_streaming)
            found = await conn.fetch(f"SELECT id FROM pg_temp.chat_messages WHERE TRUE {flt}")
            return {ids[r["id"]] for r in found}
    finally:
        await conn.close()


async def test_filter_cases():
    rows = [
        ("pipeline_c_hidden", "assistant", "pipeline_c", True, "🔄 **[세션 자동보고]** Pipeline Runner: runner-4bfb6f0d " * 10, None),
        ("partial_notice_58", "assistant", "interrupted_partial", True, NOTICE_58, None),
        ("partial_notice_llm", "assistant", "interrupted_partial", True, "⚠️ _전체 LLM 장애 — 잠시 후 다시 시도해주세요._", None),
        ("partial_body_96", "assistant", "interrupted_partial", True, BODY_96 + TAIL, None),
        ("archived_with_final", "assistant", "_archived_partial", True, "x" * 300, {"final_message_id": str(uuid.uuid4())}),
        ("archived_no_final_short", "assistant", "_archived_partial", True, "짧지만 실재하는 답변입니다.", None),
        ("system_trigger", "user", "system_trigger", True, "[시스템] 대표님 지시 주입문 " * 30, None),
        ("normal", "assistant", None, False, "hi", None),
    ]
    visible = await _visible(rows)
    assert "pipeline_c_hidden" in visible                        # (a)
    assert "partial_notice_58" not in visible                    # (b)
    assert "partial_notice_llm" not in visible
    assert "partial_body_96" in visible                          # (c)
    assert "archived_with_final" not in visible                  # (d)
    assert "archived_no_final_short" in visible
    assert "system_trigger" not in visible                       # (e)
    assert "normal" in visible


async def test_pipeline_c_also_visible_in_history_mode():
    rows = [("pc", "assistant", "pipeline_c", True, "⚠️ **[Pipeline Runner 스톨 감지]** " * 5, None)]
    assert await _visible(rows, include_streaming=False) == {"pc"}


def test_filter_has_no_length_magic_number():
    for streaming in (True, False):
        assert "> 200" not in chat_service._visible_message_filter(True, streaming)
    assert "pipeline_c" in chat_service._RUNNER_PROGRESS_INTENTS


def test_notice_phrases_are_stripped_in_sql():
    sql = chat_service._has_preserved_body_sql()
    assert "전체 LLM 장애" in sql
    assert "응답이 중단되어 여기까지 보존되었습니다" in sql


def test_history_filter_allows_substantial_hidden_assistant_body():
    sql = chat_service._visible_message_filter(False, False)
    assert "'interrupted_partial'" in sql
    assert chat_service._has_preserved_body_sql() in sql
    assert "final_message_id" in sql


def test_history_filter_has_no_streaming_placeholder_allow():
    for is_active in (True, False):
        sql = chat_service._visible_message_filter(is_active, False)
        assert "OR intent = 'streaming_placeholder'" not in sql


def test_live_filter_still_allows_streaming_placeholder():
    sql = chat_service._visible_message_filter(True, True)
    assert "OR intent = 'streaming_placeholder'" in sql
    assert chat_service._substantial_hidden_assistant_allow_sql() in sql


def test_active_history_filter_excludes_streaming_placeholder():
    sql = chat_service._visible_message_filter(True, False)
    assert "AND intent IS DISTINCT FROM 'streaming_placeholder'" in sql


async def test_history_mode_shows_hidden_body_and_hides_notice_only():
    rows = [
        ("partial_body", "assistant", "interrupted_partial", True, BODY_96 + TAIL, None),
        ("partial_notice", "assistant", "interrupted_partial", True, NOTICE_58, None),
        ("runner_body", "assistant", "runner_response", True, BODY_96, None),
        ("placeholder", "assistant", "streaming_placeholder", True, "x" * 300, None),
        ("archived_with_final", "assistant", "_archived_partial", True, "x" * 300, {"final_message_id": str(uuid.uuid4())}),
    ]
    assert await _visible(rows, include_streaming=False) == {"partial_body", "runner_body"}
