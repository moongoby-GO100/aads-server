from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from fastapi import Response

from app.services import chat_cleanup_service, chat_service
from app.routers import chat as chat_router
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


def test_runner_fast_path_prompt_block_instructs_batch_background_work():
    prompt = chat_service._runner_fast_path_prompt_block()

    assert "pipeline_runner_submit_batch" in prompt
    assert "60초 이상" in prompt
    assert "parallel_group" in prompt
    assert "depends_on_key" in prompt
    assert "XS 또는 S" in prompt
    assert "채팅 응답을 완료" in prompt


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


def test_normalize_tool_events_accepts_legacy_names_and_codex_events():
    events = chat_service.normalize_tool_events([
        "web_search",
        {"type": "tool_use", "tool_name": "run_remote_command", "tool_input": {"command": "pwd"}},
        {"type": "tool_result", "tool_name": "run_remote_command", "content": "ok"},
        {"type": "thinking", "thinking": "checking"},
    ])

    assert events == [
        {"type": "tool_use", "tool_name": "web_search", "tool_use_id": "", "tool_input": {}},
        {"type": "tool_use", "tool_name": "run_remote_command", "tool_use_id": "", "tool_input": {"command": "pwd"}},
        {"type": "tool_result", "tool_name": "run_remote_command", "tool_use_id": "", "content": "ok"},
        {"type": "thinking", "content": "checking"},
    ]


def test_actionable_quoted_instruction_is_promoted_from_missed_reply_complaint():
    content = (
        "내가 지시한 지시내용에 응답이 없는데\n"
        '"뉴스매매 데일리 이거 비활성화 하라고했는데 내가 직접 화면에서 '
        '비활성화 할건데 화면 확인하고 내가 비활성화 할수 있게 조치하고 보고해" '
        "이렇게 마지막 채팅창 대화버블에 남아있는데 왜 응답을 못하는거지?"
    )

    promoted = chat_service._promote_actionable_quoted_instruction(content)

    assert "[응답 누락 지적에서 추출한 현재 실행 지시]" in promoted
    assert "뉴스매매 데일리 이거 비활성화" in promoted
    assert "실제 처리해야 할 CEO 지시" in promoted


def test_actionable_quoted_instruction_does_not_promote_plain_quotes():
    content = '그가 "뉴스매매 데일리"라고 말했는데 의미가 뭐야?'

    assert chat_service._promote_actionable_quoted_instruction(content) == content


@pytest.mark.asyncio
async def test_list_messages_minimal_is_read_only_and_selects_light_fields():
    session_id = str(uuid.uuid4())
    tenant_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)
    message_id = uuid.uuid4()

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[
        {
            "id": message_id,
            "role": "assistant",
            "content": "요약된 응답",
            "created_at": created_at,
            "status": "completed",
        }
    ])
    conn.execute = AsyncMock()

    with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)):
        result = await chat_service.list_messages(
            session_id,
            limit=5,
            sort="desc",
            fields="minimal",
            read_only=True,
            tenant_id=tenant_id,
        )

    query = " ".join(conn.fetch.await_args.args[0].split())
    assert "SELECT id, session_id, role, LEFT(content, 200) AS content" in query
    assert "tenant_id = $4" in query
    assert "AS has_tools" in query
    assert "AS tool_count" in query
    assert "AS tool_names" in query
    assert "SELECT *" not in query
    assert "thinking_summary" not in query
    assert "embedding" not in query
    conn.execute.assert_not_awaited()
    assert result == [
        {
            "id": message_id,
            "role": "assistant",
            "content": "요약된 응답",
            "created_at": created_at,
            "status": "completed",
        }
    ]


@pytest.mark.asyncio
async def test_get_message_full_normalizes_tools_for_hydrate():
    message_id = uuid.uuid4()
    session_id = uuid.uuid4()
    tenant_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": message_id,
        "session_id": session_id,
        "role": "assistant",
        "content": "full answer",
        "tools_called": ["web_search"],
        "created_at": created_at,
    })

    with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)):
        result = await chat_service.get_message(str(message_id), tenant_id=tenant_id)

    query = " ".join(conn.fetchrow.await_args.args[0].split())
    assert "WHERE id = $1 AND tenant_id = $2" in query

    assert result["content"] == "full answer"
    assert result["tools_called"] == [
        {"type": "tool_use", "tool_name": "web_search", "tool_use_id": "", "tool_input": {}}
    ]
    assert result["has_tools"] is True
    assert result["tool_count"] == 1
    assert result["tool_names"] == ["web_search"]


def test_message_response_headers_include_timing_size_and_row_count():
    response = Response()
    payload = [{"id": str(uuid.uuid4()), "role": "user", "content": "hi"}]

    chat_router._set_message_response_headers(response, 0.0, payload)

    assert response.headers["X-Response-Time"].endswith("ms")
    assert int(response.headers["X-Payload-Bytes"]) > 0
    assert response.headers["X-Row-Count"] == "1"


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
        assert chat_service._streaming_state[session_id]["completed"] is True

        chat_service._streaming_state[session_id]["_completed_delivered_at"] = (
            chat_service._bg_time.monotonic() - 61
        )
        chat_service.get_streaming_status(session_id)
        assert session_id not in chat_service._streaming_state
    finally:
        chat_service._streaming_state.pop(session_id, None)
        chat_service._active_bg_tasks.pop(session_id, None)


@pytest.mark.asyncio
async def test_stop_session_streaming_clears_stale_interrupt_stream_flag():
    session_id = str(uuid.uuid4())
    interrupt_queue.set_streaming(session_id, True)
    interrupt_queue.push_interrupt(session_id, "다시 실행해")

    try:
        result = await chat_service.stop_session_streaming(session_id)

        assert result["stopped"] is False
        assert interrupt_queue.is_streaming(session_id) is False
        assert [item["content"] for item in interrupt_queue.pop_pending_interrupts(session_id)] == ["다시 실행해"]
    finally:
        interrupt_queue.set_streaming(session_id, False)
        interrupt_queue.pop_interrupts(session_id)
        interrupt_queue.pop_pending_interrupts(session_id)
        chat_service._streaming_state.pop(session_id, None)
        chat_service._active_bg_tasks.pop(session_id, None)


@pytest.mark.asyncio
async def test_cleanup_stale_streaming_placeholders_promotes_message_and_interrupts_execution():
    session_id = str(uuid.uuid4())
    execution_id = str(uuid.uuid4())
    message_id = uuid.uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": message_id,
            "session_id": session_id,
            "execution_id": execution_id,
            "content": "부분 응답입니다.\n\n⏳ _생성 중..._",
        }
    ])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    chat_service._streaming_state[session_id] = {
        "content": "",
        "completed": True,
        "started_at": chat_service._bg_time.monotonic(),
    }

    try:
        with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)):
            result = await chat_service.cleanup_stale_streaming_placeholders(timeout_sec=600)

        executed_sql = [" ".join(call.args[0].split()) for call in conn.execute.await_args_list]
        assert result["cleaned"] == 1
        assert result["promoted"] == 1
        assert any("intent = 'interrupted_partial'" in sql for sql in executed_sql)
        assert any("UPDATE chat_turn_executions" in sql and "status = 'interrupted'" in sql for sql in executed_sql)
        assert any("UPDATE chat_sessions" in sql and "current_execution_id = NULL" in sql for sql in executed_sql)
        assert chat_service._streaming_state[session_id]["completed"] is True
        assert "부분 응답입니다." in chat_service._streaming_state[session_id]["content"]
    finally:
        chat_service._streaming_state.pop(session_id, None)
        chat_service._active_bg_tasks.pop(session_id, None)


@pytest.mark.asyncio
async def test_cleanup_stale_streaming_placeholders_skips_live_session():
    session_id = str(uuid.uuid4())
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": uuid.uuid4(),
            "session_id": session_id,
            "execution_id": None,
            "content": "아직 실행 중",
        }
    ])
    conn.fetchval = AsyncMock()
    conn.execute = AsyncMock()
    chat_service._active_bg_tasks[session_id] = SimpleNamespace(done=lambda: False)

    try:
        with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)):
            result = await chat_service.cleanup_stale_streaming_placeholders(timeout_sec=600)

        assert result["cleaned"] == 0
        assert result["skipped_active"] == 1
        conn.fetchval.assert_not_awaited()
        conn.execute.assert_not_awaited()
    finally:
        chat_service._active_bg_tasks.pop(session_id, None)
        chat_service._streaming_state.pop(session_id, None)


@pytest.mark.asyncio
async def test_cleanup_overlong_running_executions_closes_live_task():
    session_id = str(uuid.uuid4())
    execution_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {
            "execution_id": execution_id,
            "session_id": session_id,
            "assistant_message_id": message_id,
            "partial_content": "오래 걸린 부분 응답",
            "age_seconds": 3600,
        }
    ])
    cancelled = {"called": False, "msg": None}
    task = SimpleNamespace(
        done=lambda: False,
        cancel=lambda msg=None: cancelled.update({"called": True, "msg": msg}),
    )
    chat_service._active_bg_tasks[session_id] = task
    chat_service._streaming_state[session_id] = {
        "content": "메모리의 오래 걸린 부분 응답",
        "completed": False,
        "started_at": chat_service._bg_time.monotonic() - 3600,
    }

    try:
        with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)), patch(
            "app.services.chat_service._mark_execution_interrupted", new_callable=AsyncMock
        ) as mark_interrupted:
            result = await chat_service.cleanup_overlong_running_executions(timeout_sec=2700)

        assert result["closed"] == 1
        assert result["cancelled_active"] == 1
        assert cancelled == {"called": True, "msg": "active_stream_hard_timeout"}
        assert session_id not in chat_service._active_bg_tasks
        assert session_id not in chat_service._streaming_state
        mark_interrupted.assert_awaited_once()
        args, kwargs = mark_interrupted.await_args
        assert args[1] == session_id
        assert args[2] == execution_id
        assert args[3].startswith("active_stream_hard_timeout_after_2700s")
        assert "age=3600s" in args[3]
        assert "timeout=2700s" in args[3]
        assert "content_len=16" in args[3]
        assert kwargs["partial_content"] == "메모리의 오래 걸린 부분 응답"
        assert kwargs["placeholder_id"] == message_id
    finally:
        chat_service._active_bg_tasks.pop(session_id, None)
        chat_service._streaming_state.pop(session_id, None)


def test_incomplete_progress_tail_is_not_completion_candidate():
    assert chat_service._looks_like_incomplete_progress_tail(
        "핵심 파일을 즉시 읽고 수정합니다. MCP 도구를 로드합니다."
    )
    assert chat_service._looks_like_incomplete_progress_tail(
        "DB 상태를 추가 확인하겠습니다."
    )
    assert chat_service._looks_like_incomplete_progress_tail(
        "응답 일부\n\n⏳ 생성 중..."
    )


def test_final_report_tail_is_completion_candidate():
    assert not chat_service._looks_like_incomplete_progress_tail(
        "수행 내역: 완료판정 가드를 적용했습니다.\n"
        "검증: pytest 통과.\n"
        "미완료: 브라우저 E2E는 미실행입니다."
    )


@pytest.mark.asyncio
async def test_delete_streaming_placeholder_marks_final_missing_as_interrupted_partial():
    session_id = str(uuid.uuid4())
    execution_id = str(uuid.uuid4())
    placeholder_id = uuid.uuid4()
    placeholder_created_at = datetime.now(timezone.utc)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": placeholder_id,
        "content": "부분 응답입니다.\n\n⏳ _생성 중..._",
        "created_at": placeholder_created_at,
    })
    conn.fetchval = AsyncMock(side_effect=[
        None,  # live execution
        placeholder_created_at - timedelta(seconds=2),  # last user before placeholder
        0,  # final assistant exists
        0,  # duplicate recovered exists
    ])
    conn.execute = AsyncMock()

    with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)):
        await chat_service._delete_streaming_placeholder(session_id, execution_id)

    executed_sql = [" ".join(call.args[0].split()) for call in conn.execute.await_args_list]
    assert any("intent = 'interrupted_partial'" in sql for sql in executed_sql)
    assert any("UPDATE chat_turn_executions" in sql and "status = 'interrupted'" in sql for sql in executed_sql)
    assert not any("intent = NULL, model_used = 'interrupted'" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_save_interrupted_partial_binds_missing_execution_to_active_execution():
    session_id = str(uuid.uuid4())
    execution_id = uuid.uuid4()
    placeholder_id = uuid.uuid4()
    created_at = datetime.now(timezone.utc)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=[
        execution_id,  # _resolve_stream_execution_binding: current/recent running execution
        placeholder_id,  # _resolve_stream_execution_binding: placeholder for execution
    ])
    conn.fetchrow = AsyncMock(return_value={
        "id": placeholder_id,
        "created_at_text": created_at.isoformat(),
    })
    conn.execute = AsyncMock()

    with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)):
        result = await chat_service._save_interrupted_partial_message(
            session_id,
            "부분 응답입니다.",
            reason="background_producer_incomplete_exit",
        )

    assert result is not None
    assert result["execution_id"] == str(execution_id)
    executed_sql = [" ".join(call.args[0].split()) for call in conn.execute.await_args_list]
    assert any("UPDATE chat_turn_executions" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_save_interrupted_partial_blocks_orphan_insert_without_execution():
    session_id = str(uuid.uuid4())
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=[
        None,  # active execution
        None,  # session placeholder
        None,  # orphan placeholder to mark
    ])
    conn.fetchrow = AsyncMock()
    conn.execute = AsyncMock()

    with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)):
        result = await chat_service._save_interrupted_partial_message(
            session_id,
            "실행 연결이 없는 부분 응답입니다.",
            reason="background_producer_incomplete_exit",
        )

    assert result is None
    executed_sql = [" ".join(call.args[0].split()) for call in conn.fetchrow.await_args_list]
    assert not any("INSERT INTO chat_messages" in sql and "interrupted_partial" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_cleanup_deleted_duplicate_messages_dry_run_does_not_delete():
    session_id = uuid.uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {"id": uuid.uuid4(), "session_id": session_id},
    ])
    conn.fetchval = AsyncMock(return_value=12)
    conn.execute = AsyncMock()

    with patch("app.services.chat_cleanup_service.get_pool", return_value=_Pool(conn)):
        result = await chat_cleanup_service.cleanup_deleted_duplicate_messages(
            retention_days=7,
            batch_size=100,
            dry_run=True,
        )

    assert result["eligible"] == 12
    assert result["deleted"] == 0
    assert result["sessions_touched"] == 1
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_deleted_duplicate_messages_deletes_and_recounts_sessions():
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[
        [{"id": message_id, "session_id": session_id}],
        [{"session_id": session_id}],
    ])
    conn.fetchval = AsyncMock(return_value=1)
    conn.execute = AsyncMock()

    with patch("app.services.chat_cleanup_service.get_pool", return_value=_Pool(conn)):
        result = await chat_cleanup_service.cleanup_deleted_duplicate_messages(
            retention_days=7,
            batch_size=100,
        )

    executed_sql = [" ".join(call.args[0].split()) for call in conn.execute.await_args_list]
    assert result["deleted"] == 1
    assert result["sessions_touched"] == 1
    assert any("UPDATE chat_sessions" in sql and "message_count" in sql for sql in executed_sql)


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

    assert any('"html_context_used": true' in chunk for chunk in chunks)
    assert "[현재 작업 중인 HTML 아티팩트" in captured["system_prompt"]
    assert "```html" in captured["system_prompt"]
    assert "<button>Old</button>" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_multistep_request_injects_todo_prompt_block():
    captured = {}
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

    async def _mock_call_stream(*, system_prompt, **kwargs):
        captured["system_prompt"] = system_prompt
        yield {"type": "error", "content": "stop"}

    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
        patch("app.services.chat_service.create_trace", return_value=None),
        patch("app.services.chat_service._get_last_html_artifact", new=AsyncMock(return_value=None)),
        patch(
            "app.services.context_builder.build_messages_context",
            new=AsyncMock(return_value=([{"role": "user", "content": "1. migration 추가 2. chat_service 통합"}], "BASE_SYSTEM")),
        ),
        patch(
            "app.services.intent_router.classify",
            new=AsyncMock(return_value=SimpleNamespace(
                intent="code_modify",
                model="claude-sonnet",
                use_tools=True,
                tool_group="all",
                use_gemini_direct=False,
                gemini_mode=None,
            )),
        ),
        patch("app.services.contradiction_detector.detect_contradictions", new=AsyncMock(return_value="")),
        patch("app.services.chat_embedding_service.embed_texts", new=AsyncMock(return_value=[])),
        patch("app.services.semantic_cache.SemanticCache.lookup", new=AsyncMock(return_value=None)),
        patch("app.services.chat_service._save_message", new=AsyncMock(return_value={"id": uuid.uuid4()})),
        patch("app.services.chat_service._save_and_update_session", new=AsyncMock(return_value=None)),
        patch("app.services.chat_todo_service.should_create_todos", return_value=True),
        patch("app.services.chat_todo_service.extract_todo_titles", return_value=["migration 추가", "chat_service 통합"]),
        patch(
            "app.services.chat_todo_service.create_todo_items",
            new=AsyncMock(return_value=[
                {"id": uuid.uuid4(), "title": "migration 추가"},
                {"id": uuid.uuid4(), "title": "chat_service 통합"},
            ]),
        ) as mocked_create,
        patch("app.services.model_selector.call_stream", new=_mock_call_stream),
    ):
        chunks = []
        async for chunk in chat_service.send_message_stream(
            session_id=session_id,
            content="1. migration 추가 2. chat_service 통합",
            attachments=[],
        ):
            chunks.append(chunk)

    mocked_create.assert_awaited_once()
    assert "[세션 TODO 운영 규칙]" in captured["system_prompt"]
    assert "migration 추가" in captured["system_prompt"]
    assert "chat_service 통합" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_prepare_turn_todo_context_fails_open_when_schema_missing():
    with (
        patch("app.services.chat_todo_service.should_create_todos", return_value=True),
        patch("app.services.chat_todo_service.extract_todo_titles", return_value=["migration 추가"]),
        patch(
            "app.services.chat_todo_service.create_todo_items",
            new=AsyncMock(side_effect=chat_service.asyncpg.UndefinedTableError("chat_todo_items missing")),
        ),
    ):
        result = await chat_service._prepare_turn_todo_context(
            session_id=str(uuid.uuid4()),
            content="migration 추가",
            intent="code_modify",
            use_tools=True,
            execution_id=str(uuid.uuid4()),
            message_id=str(uuid.uuid4()),
            intent_override=None,
        )

    assert result is None


@pytest.mark.asyncio
async def test_todo_completion_gate_appends_missing_note():
    session_id = str(uuid.uuid4())
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    token = chat_service._current_todo_context.set({
        "session_id": session_id,
        "execution_id": str(uuid.uuid4()),
        "resume_session_scope": False,
    })
    try:
        with (
            patch(
                "app.services.chat_todo_service.list_todo_items",
                new=AsyncMock(return_value=[
                    {"id": first_id, "title": "migration 추가", "metadata": {}},
                    {"id": second_id, "title": "chat_service 통합", "metadata": {}},
                ]),
            ),
            patch(
                "app.services.chat_todo_service.evaluate_todo_completion",
                return_value={
                    "completed_ids": [],
                    "missing_items": [{"id": second_id, "title": "chat_service 통합", "metadata": {}}],
                    "missing_titles": ["chat_service 통합"],
                    "all_completed": False,
                    "has_failure_signal": False,
                },
            ),
            patch("app.services.chat_todo_service.mark_complete", new=AsyncMock()),
            patch("app.services.chat_todo_service.update_todo_item", new=AsyncMock()) as mocked_update,
        ):
            content, gate = await chat_service._apply_todo_completion_gate(
                session_id=uuid.UUID(session_id),
                content="migration 추가 완료했습니다.",
                tools_called=[],
            )
    finally:
        chat_service._current_todo_context.reset(token)

    assert gate["missing_titles"] == ["chat_service 통합"]
    assert "[세션 TODO 점검]" in content
    assert "chat_service 통합" in content
    mocked_update.assert_awaited()


@pytest.mark.asyncio
async def test_deferred_interrupt_rewrites_no_tool_stream_before_save():
    session_id = str(uuid.uuid4())
    execution_id = uuid.uuid4()
    conn = AsyncMock()

    async def _mock_fetchrow(query, *args):
        if "model_used = 'interrupted'" in query and "content = $2" in query:
            return None
        if "INSERT INTO chat_messages" in query and "RETURNING id, created_at::text AS created_at_text" in query:
            return {"id": uuid.uuid4(), "created_at_text": datetime.now(timezone.utc).isoformat()}
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


def test_keyword_fallback_routes_only_explicit_discussion_queries():
    from app.services.intent_router import _keyword_fallback, is_explicit_debate_request

    result = _keyword_fallback("이 안건 다관점 토론해봐")

    assert result.intent == "discussion"
    assert is_explicit_debate_request("이 안건 다관점 토론해봐")

    comparison = _keyword_fallback("이 안건 장단점 비교해봐")
    assert comparison.intent == "cto_strategy"
    assert not is_explicit_debate_request("인텐트 문제 조치해. 다관점 토론은 명시 지시 때만")
    meta_request = _keyword_fallback("다관점 토론은 내가 정확하게 지시 할때 진행되게 조치해")
    assert meta_request.intent == "code_modify"


def test_broad_tool_group_excludes_run_debate():
    from app.services.tool_registry import ToolRegistry

    tools = ToolRegistry().get_tools("all")
    assert "run_debate" not in {tool["name"] for tool in tools}


@pytest.mark.asyncio
async def test_discussion_endpoint_proxies_structured_result():
    session_id = uuid.uuid4()
    payload = {
        "question": "다관점 토론해봐",
        "message": "## 종합 결론\n\n추천안",
        "synthesis": "추천안",
        "perspectives": [{"name": "기술", "analysis": "구현 가능", "key_points": ["속도"]}],
        "cost_usd": 1.23,
        "duration_ms": 4567,
        "debate_id": "debate-1234",
    }

    with (
        patch("app.routers.chat.svc.get_session", new=AsyncMock(return_value={"id": str(session_id)})),
        patch("app.routers.chat.svc.run_discussion", new=AsyncMock(return_value=payload)) as mocked_run,
    ):
        result = await chat_router.run_discussion(
            session_id,
            chat_router.DiscussionRequest(
                content="다관점 토론해봐",
                context="배경",
                perspectives=[{"name": "기술"}],
            ),
            context={"tenant": {"id": str(uuid.uuid4())}},
        )

    assert result == payload
    mocked_run.assert_awaited_once_with(
        str(session_id),
        "다관점 토론해봐",
        context="배경",
        perspectives=[{"name": "기술"}],
    )


@pytest.mark.asyncio
async def test_send_message_stream_discussion_branch_uses_orchestrator():
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
        if "SELECT settings FROM chat_users" in query:
            return {"settings": {}}
        return None

    conn.fetchrow = AsyncMock(side_effect=_mock_fetchrow)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=execution_id)

    orchestrated = {
        "question": "다관점 토론해봐",
        "message": "## 종합 결론\n\n추천안",
        "synthesis": "추천안",
        "perspectives": [{"name": "기술", "analysis": "구현 가능", "key_points": ["속도"]}],
        "cost_usd": 1.5,
        "duration_ms": 3200,
        "debate_id": "debate-5678",
        "tools_called": [{"type": "tool_use", "tool_name": "run_debate", "tool_use_id": "debate-5678", "tool_input": {}}],
    }
    saved = AsyncMock(return_value=None)

    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
        patch("app.services.chat_service.create_trace", return_value=None),
        patch("app.services.chat_service.get_html_edit_context_state", new=AsyncMock(return_value={"html_context_used": False})),
        patch(
            "app.services.context_builder.build_messages_context",
            new=AsyncMock(return_value=([{"role": "user", "content": "다관점 토론해봐"}], "BASE_SYSTEM")),
        ),
        patch(
            "app.services.intent_router.classify",
            new=AsyncMock(return_value=SimpleNamespace(
                intent="discussion",
                model="claude-opus",
                use_tools=False,
                tool_group="",
                use_gemini_direct=False,
                gemini_mode="",
                naver_type="",
            )),
        ),
        patch("app.services.contradiction_detector.detect_contradictions", new=AsyncMock(return_value="")),
        patch("app.services.chat_embedding_service.embed_texts", new=AsyncMock(return_value=[])),
        patch("app.services.semantic_cache.SemanticCache.lookup", new=AsyncMock(return_value=None)),
        patch("app.services.chat_service._save_message", new=AsyncMock(return_value={"id": uuid.uuid4()})),
        patch("app.services.chat_service._save_and_update_session", new=saved),
        patch("app.services.chat_service._execute_discussion_orchestrator", new=AsyncMock(return_value=orchestrated)) as mocked_orchestrator,
    ):
        chunks = []
        async for chunk in chat_service.send_message_stream(
            session_id=session_id,
            content="다관점 토론해봐",
            attachments=[],
        ):
            chunks.append(chunk)

    mocked_orchestrator.assert_awaited_once()
    assert any('"type": "thinking"' in chunk for chunk in chunks)
    assert any('"type": "delta"' in chunk for chunk in chunks)
    assert any('"debate_id": "debate-5678"' in chunk for chunk in chunks)
    assert saved.await_args.args[1] == "## 종합 결론\n\n추천안"


def test_strip_internal_continuation_context_removes_continue_scaffold():
    content = (
        "이어서 진행해\n\n"
        "[이전 응답이 중단되었습니다. 아래 부분까지 생성되었으니 이어서 작성해주세요]\n"
        "이전 assistant 본문\n"
        "[위 내용에 이어서 자연스럽게 계속 작성하세요. 중복 없이 이어서.]"
    )

    assert chat_service._strip_internal_continuation_context(content) == "이어서 진행해"


def test_strip_internal_continuation_context_preserves_visible_instruction_before_reply_quote():
    content = (
        "[이전 추가 지시] 관리자 메뉴 반영해\n\n"
        "[CEO가 지정한 이전 AI 응답 (reply_to)]\n"
        "이전 assistant 본문\n\n"
        "[CEO 추가 지시]\n"
        "관리자 메뉴 확인해"
    )

    assert (
        chat_service._strip_internal_continuation_context(content)
        == "[이전 추가 지시] 관리자 메뉴 반영해"
    )


def test_strip_internal_continuation_context_extracts_instruction_from_reply_quote_wrapper():
    content = (
        "[CEO가 지정한 이전 AI 응답 (reply_to)]\n"
        "이전 assistant 본문\n\n"
        "[CEO 추가 지시]\n"
        "이어서 진행해"
    )

    assert chat_service._strip_internal_continuation_context(content) == "이어서 진행해"


def test_strip_internal_continuation_context_defaults_reply_only_to_continue_instruction():
    content = (
        "[CEO가 지정한 이전 AI 응답 (reply_to)]\n"
        "이전 assistant 본문만 저장된 오래된 오염 메시지"
    )

    assert chat_service._strip_internal_continuation_context(content) == "이어서 진행해"


def test_strip_internal_continuation_context_removes_nested_scaffolds():
    content = (
        "[CEO가 지정한 이전 AI 응답 (reply_to)]\n"
        "이전 assistant 본문\n\n"
        "[CEO 추가 지시]\n"
        "이어서 진행해\n\n"
        "[이전 응답이 중단되었습니다. 아래 부분까지 생성되었으니 이어서 작성해주세요]\n"
        "[CEO가 지정한 이전 AI 응답 (reply_to)]\n"
        "중첩된 assistant 본문\n\n"
        "[CEO 추가 지시]\n"
        "중첩 지시"
    )

    assert chat_service._strip_internal_continuation_context(content) == "이어서 진행해"
