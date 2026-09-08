"""Status polling must not terminate a relay retry from its saved partial."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.routers import chat as chat_router


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_used", ["interrupted", "stopped"])
@pytest.mark.parametrize("execution_status", ["running", "retrying"])
@pytest.mark.parametrize("live_runtime,lease_valid", [(True, False), (False, True), (False, False)])
async def test_terminal_message_projection_never_revokes_execution(
    model_used, execution_status, live_runtime, lease_valid
):
    session_id, execution_id, assistant_id = uuid4(), uuid4(), uuid4()
    partial = "조사 결과를 보존했습니다.\n\n_(이전 응답은 중단 처리되었습니다. 최신 지시를 우선 처리합니다.)_"
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "execution_id": str(execution_id),
        "status": execution_status,
        "user_content": "응답 끊김을 조치해 주세요",
        "assistant_model_used": model_used,
        "lease_valid": lease_valid,
        "partial_content": partial,
        "tools_called": [],
        "last_event_id": "123-0",
        "final_message_id": str(assistant_id),
    }

    async def finalize(_session_id, payload, _conn=None):
        return payload

    with (
        patch.object(chat_router.svc, "get_session", AsyncMock(return_value={"id": str(session_id)})),
        patch.object(chat_router.svc, "get_streaming_status", return_value=None),
        patch.object(chat_router, "_has_live_streaming_runtime", return_value=live_runtime),
        patch.object(chat_router, "_finalize_streaming_status", side_effect=finalize),
        patch.object(chat_router, "_settle_stale_execution_for_recovery", AsyncMock()) as settle,
        patch.object(chat_router, "_ensure_running_placeholder_anchor", AsyncMock()) as repair,
        patch("app.core.db_pool.get_pool", return_value=_Pool(conn)),
    ):
        result = await chat_router.get_streaming_status(
            session_id, context={"tenant": {"id": str(uuid4())}}
        )

    assert result["is_streaming"] is (live_runtime or lease_valid)
    assert result["stream_status"] == (
        "recovering" if live_runtime or lease_valid else "needs_continuation"
    )
    assert result["partial_content"] == partial
    assert result["content_length"] == len(partial)
    assert result["execution_id"] == str(execution_id)
    assert result["final_message_ready"] is False
    # Both an active remote owner and an expired orphan are read-only here:
    # only the fenced worker may claim/settle the orphan after the SELECT.
    conn.execute.assert_not_awaited()
    conn.fetchval.assert_not_awaited()
    settle.assert_not_awaited()
    repair.assert_not_awaited()
    assert "te.lease_expires_at > NOW()" in conn.fetchrow.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("live_runtime,lease_valid", [(True, False), (False, True)])
@pytest.mark.parametrize("partial", ["", "분석이 진행 중입니다"])
async def test_stale_settle_keeps_live_owner_even_beyond_hard_cutoff(
    live_runtime, lease_valid, partial
):
    conn = AsyncMock()
    execution = {
        "execution_id": str(uuid4()),
        "status": "running",
        "lease_valid": lease_valid,
        "partial_content": partial,
        "tools_called": [],
        "last_event_id": None,
        "updated_age_seconds": 1000,
        "started_age_seconds": 10000,
        "updated_recently": False,
    }
    result = await chat_router._settle_stale_execution_for_recovery(
        conn, uuid4(), execution, has_live_runtime=live_runtime
    )
    assert result is None
    conn.execute.assert_not_awaited()
    conn.fetchval.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_last_response_keeps_remote_leased_execution_running():
    session_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "execution_id": str(uuid4()),
        "status": "running",
        "lease_valid": True,
        "placeholder_id": str(uuid4()),
        "partial_content": "응답 분석 중",
        "tools_called": [],
        "last_event_id": None,
        "updated_age_seconds": 1000,
        "started_age_seconds": 10000,
        "updated_recently": False,
    }
    with (
        patch.object(chat_router.svc, "get_session", AsyncMock(return_value={"id": str(session_id)})),
        patch.object(chat_router, "_has_live_streaming_runtime", return_value=False),
        patch("app.core.db_pool.get_pool", return_value=_Pool(conn)),
    ):
        result = await chat_router.get_last_response(
            session_id, context={"tenant": {"id": str(uuid4())}}
        )
    assert result == {"found": False, "generating": True}
    conn.execute.assert_not_awaited()
    conn.fetchval.assert_not_awaited()
    assert "te.lease_expires_at > NOW()" in conn.fetchrow.await_args.args[0]
