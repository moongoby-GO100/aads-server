from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.services import chat_service
from app.core import interrupt_queue


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


@pytest.mark.asyncio
async def test_get_last_html_artifact():
    session_id = str(uuid.uuid4())
    artifact_id = uuid.uuid4()
    message_id = uuid.uuid4()
    created_at = datetime.now(timezone.utc)

    empty_conn = AsyncMock()
    empty_conn.fetchrow = AsyncMock(return_value=None)
    with patch("app.services.chat_service.get_pool", return_value=_Pool(empty_conn)):
        assert await chat_service._get_last_html_artifact(session_id) is None

    row_conn = AsyncMock()
    row_conn.fetchrow = AsyncMock(return_value={
        "id": artifact_id,
        "title": "Landing Page",
        "content": "<html>preview</html>",
        "message_id": message_id,
        "created_at": created_at,
    })
    with patch("app.services.chat_service.get_pool", return_value=_Pool(row_conn)):
        result = await chat_service._get_last_html_artifact(session_id)

    assert result == {
        "id": artifact_id,
        "title": "Landing Page",
        "content": "<html>preview</html>",
        "message_id": message_id,
        "created_at": created_at,
    }


def test_edit_intent_detection():
    assert chat_service._is_html_edit_intent("이 부분 파란색으로 바꿔")
    assert chat_service._is_html_edit_intent("make the button bigger")
    assert not chat_service._is_html_edit_intent("이 HTML의 구조를 설명해줘")
    assert not chat_service._is_html_edit_intent("")


def test_dedupe_recovery_like_messages_keeps_longest_recovery_message():
    shared_prefix = "partial prefix answer " * 3
    messages = [
        {"id": "u1", "role": "user", "content": "질문", "model_used": None},
        {"id": "a1", "role": "assistant", "content": f"{shared_prefix}A", "model_used": "recovered"},
        {"id": "a2", "role": "assistant", "content": f"{shared_prefix}A with more detail", "model_used": "recovered_from_redis"},
        {"id": "a3", "role": "assistant", "content": "최종 정상 응답", "model_used": "gpt-5.4"},
    ]

    deduped = chat_service._dedupe_recovery_like_messages(messages)

    assert [message["id"] for message in deduped] == ["u1", "a2", "a3"]


def test_streaming_progress_markers_are_not_meaningful_partial_content():
    progress_only = (
        "⚠️ _GPT-5.5 (Codex CLI) 연결이 일시 중단되어 자동 재시도합니다._\n\n"
        "⏳ _AI가 응답을 생성 중입니다... (도구 0회 호출 중)_"
    )

    assert not chat_service._has_meaningful_partial_content(progress_only)
    assert chat_service._has_meaningful_partial_content("원인 분석 보고입니다.\n\n⏳ _생성 중..._")


def test_terminal_interrupt_marker_completes_memory_stream_once():
    session_id = str(uuid.uuid4())
    chat_service._streaming_state[session_id] = {
        "content": "부분 응답\n\n_(이전 응답은 중단 처리되었습니다. 최신 지시를 우선 처리합니다.)_",
        "started_at": chat_service._bg_time.monotonic(),
        "completed": False,
        "execution_id": str(uuid.uuid4()),
    }
    try:
        status = chat_service.get_streaming_status(session_id)
        assert status["is_streaming"] is False
        assert status["just_completed"] is True
        assert session_id not in chat_service._streaming_state
    finally:
        chat_service._streaming_state.pop(session_id, None)
        chat_service._active_bg_tasks.pop(session_id, None)


@pytest.mark.asyncio
async def test_newer_user_message_supersedes_running_execution():
    execution_user_id = str(uuid.uuid4())
    latest_user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "execution_user_id": execution_user_id,
        "execution_user_created_at": now,
        "latest_user_id": latest_user_id,
        "latest_user_created_at": now + timedelta(seconds=1),
    })

    assert await chat_service._execution_has_newer_user_message(
        conn,
        str(uuid.uuid4()),
        str(uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_additional_instruction_message_does_not_supersede_execution():
    execution_user_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "execution_user_id": execution_user_id,
        "execution_user_created_at": now,
        "latest_user_id": execution_user_id,
        "latest_user_created_at": now,
    })

    assert not await chat_service._execution_has_newer_user_message(
        conn,
        str(uuid.uuid4()),
        str(uuid.uuid4()),
    )
    query = conn.fetchrow.await_args.args[0]
    assert "content NOT LIKE '[추가 지시]%%'" in query


@pytest.mark.asyncio
async def test_interrupt_execution_for_newer_user_marks_terminal_and_clears_current():
    session_id = str(uuid.uuid4())
    execution_id = str(uuid.uuid4())
    placeholder_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "execution_user_id": str(uuid.uuid4()),
        "execution_user_created_at": now,
        "latest_user_id": str(uuid.uuid4()),
        "latest_user_created_at": now + timedelta(seconds=1),
    })
    conn.fetchval = AsyncMock(return_value=placeholder_id)
    conn.execute = AsyncMock()

    assert await chat_service._interrupt_execution_if_newer_user(
        conn,
        session_id,
        execution_id,
        partial_content="",
        placeholder_id=str(placeholder_id),
    )

    executed_sql = [" ".join(call.args[0].split()) for call in conn.execute.await_args_list]
    assert any(sql.startswith("DELETE FROM chat_messages WHERE id = $1") for sql in executed_sql)
    assert any("UPDATE chat_turn_executions" in sql and "status = 'interrupted'" in sql for sql in executed_sql)
    assert any("UPDATE chat_sessions" in sql and "current_execution_id = NULL" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_html_context_injection():
    captured = {}
    session_id = str(uuid.uuid4())
    artifact_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    conn = AsyncMock()

    async def _mock_fetchrow(query, *args):
        if "FROM chat_messages WHERE idempotency_key" in query:
            return None
        if "WHERE session_id = $1 AND role = 'user' AND content = $2" in query:
            return None
        if "FROM chat_workspaces w" in query:
            return {
                "workspace_id": uuid.uuid4(),
                "workspace_name": "AADS",
                "system_prompt": "BASE_SYSTEM",
                "workspace_settings": {},
                "role_key": "",
                "session_settings": {},
            }
        if "FROM chat_session_stats" in query:
            return {"cost_total": 0, "message_count": 0}
        if "SELECT settings FROM chat_users" in query:
            return {"settings": {}}
        return None

    conn.fetchrow = AsyncMock(side_effect=_mock_fetchrow)
    conn.fetch = AsyncMock(return_value=[])

    async def _mock_call_stream(*, system_prompt, **kwargs):
        captured["system_prompt"] = system_prompt
        yield {"type": "error", "content": "stop"}

    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
        patch("app.services.chat_service.create_trace", return_value=None),
        patch(
            "app.services.chat_service._get_last_html_artifact",
            new=AsyncMock(return_value={
                "id": artifact_id,
                "title": "Preview",
                "content": "<html><body><button>Old</button></body></html>",
                "message_id": uuid.uuid4(),
                "created_at": now - timedelta(hours=1),
            }),
        ),
        patch(
            "app.services.context_builder.build_messages_context",
            new=AsyncMock(return_value=([{"role": "user", "content": "버튼 크기 키워"}], "BASE_SYSTEM")),
        ),
        patch(
            "app.services.intent_router.classify",
            new=AsyncMock(return_value=SimpleNamespace(
                intent="general",
                model="claude-sonnet",
                use_tools=False,
                tool_group=None,
                use_gemini_direct=False,
                gemini_mode=None,
            )),
        ),
        patch(
            "app.services.contradiction_detector.detect_contradictions",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "app.services.chat_embedding_service.embed_texts",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.services.semantic_cache.SemanticCache.lookup",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.chat_service._save_message",
            new=AsyncMock(return_value={"id": uuid.uuid4()}),
        ),
        patch(
            "app.services.chat_service._save_and_update_session",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.model_selector.call_stream",
            new=_mock_call_stream,
        ),
    ):
        chunks = []
        async for chunk in chat_service.send_message_stream(
            session_id=session_id,
            content="버튼 크기 키워",
            attachments=[],
        ):
            chunks.append(chunk)

    assert '"html_context_used": true' in chunks[0]
    assert "[현재 작업 중인 HTML 아티팩트" in captured["system_prompt"]
    assert "```html" in captured["system_prompt"]
    assert "<button>Old</button>" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_deferred_interrupt_rewrites_no_tool_stream_before_save():
    session_id = str(uuid.uuid4())
    execution_id = uuid.uuid4()
    conn = AsyncMock()

    async def _mock_fetchrow(query, *args):
        if "FROM chat_messages WHERE idempotency_key" in query:
            return None
        if "WHERE session_id = $1 AND role = 'user' AND content = $2" in query:
            return None
        if "FROM chat_workspaces w" in query:
            return {
                "workspace_id": uuid.uuid4(),
                "workspace_name": "AADS",
                "system_prompt": "BASE_SYSTEM",
                "workspace_settings": {},
                "role_key": "",
                "session_settings": {},
            }
        if "FROM chat_session_stats" in query:
            return {"cost_total": 0, "message_count": 0}
        if "SELECT settings FROM chat_users" in query:
            return {"settings": {}}
        return None

    conn.fetchrow = AsyncMock(side_effect=_mock_fetchrow)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=execution_id)

    call_count = 0

    async def _mock_call_stream(*, session_id: str = "", **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield {"type": "delta", "content": "초안 답변"}
            interrupt_queue.push_interrupt(session_id, "표 형태로 다시 정리해")
            yield {"type": "done", "model": "claude-sonnet", "cost": "0.01", "input_tokens": 1, "output_tokens": 2}
        else:
            assert any(
                message["role"] == "user" and "표 형태로 다시 정리해" in str(message["content"])
                for message in kwargs["messages"]
            )
            yield {"type": "delta", "content": "수정본 답변"}
            yield {"type": "done", "model": "claude-sonnet", "cost": "0.02", "input_tokens": 3, "output_tokens": 4}

    saved = AsyncMock(return_value=None)
    try:
        with (
            patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
            patch("app.services.chat_service.create_trace", return_value=None),
            patch("app.services.chat_service._get_last_html_artifact", new=AsyncMock(return_value=None)),
            patch(
                "app.services.context_builder.build_messages_context",
                new=AsyncMock(return_value=([{"role": "user", "content": "보고해"}], "BASE_SYSTEM")),
            ),
            patch(
                "app.services.intent_router.classify",
                new=AsyncMock(return_value=SimpleNamespace(
                    intent="general",
                    model="claude-sonnet",
                    use_tools=False,
                    tool_group=None,
                    use_gemini_direct=False,
                    gemini_mode=None,
                )),
            ),
            patch("app.services.contradiction_detector.detect_contradictions", new=AsyncMock(return_value="")),
            patch("app.services.chat_embedding_service.embed_texts", new=AsyncMock(return_value=[])),
            patch("app.services.semantic_cache.SemanticCache.lookup", new=AsyncMock(return_value=None)),
            patch("app.services.output_validator.validate_response", return_value=SimpleNamespace(is_valid=True)),
            patch("app.services.response_critic.critique_response", new=AsyncMock(return_value=None)),
            patch("app.services.chat_service._save_message", new=AsyncMock(return_value={"id": uuid.uuid4()})),
            patch("app.services.chat_service._save_and_update_session", new=saved),
            patch("app.services.model_selector.call_stream", new=_mock_call_stream),
        ):
            chunks = []
            async for chunk in chat_service.send_message_stream(
                session_id=session_id,
                content="보고해",
                attachments=[],
            ):
                chunks.append(chunk)
    finally:
        interrupt_queue.pop_interrupts(session_id)
        interrupt_queue.pop_pending_interrupts(session_id)

    assert call_count == 2
    assert any("interrupt_applied" in chunk for chunk in chunks)
    assert any("stream_reset" in chunk for chunk in chunks)
    assert saved.await_args.args[1] == "수정본 답변"
