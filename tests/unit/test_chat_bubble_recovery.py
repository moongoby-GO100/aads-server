"""Regression cases for timeout bubble loss and superseded resume storms."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services import chat_service as svc


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduler", ["interrupted", "recovery"])
async def test_live_task_rejects_resume_before_any_database_access(monkeypatch, scheduler):
    from app.routers import chat

    sid, eid, pid = uuid4(), uuid4(), uuid4()
    conn = AsyncMock()
    monkeypatch.setattr(svc, "_is_local_active_api_slot", lambda: True)
    monkeypatch.setattr(svc, "_active_bg_tasks", {str(sid): SimpleNamespace(done=lambda: False)})
    if scheduler == "interrupted":
        resumed = await svc._schedule_interrupted_auto_resume(
            conn, str(sid), str(eid), pid, "", "llm_first_response_timeout_after_198s",
        )
    else:
        resumed = await chat._schedule_recovery_auto_resume(conn, sid, eid, pid, "")
    assert resumed is False
    assert conn.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "background_producer_incomplete_exit:CancelledError:mark_interrupted:llm_first_response_timeout_after_198s",
    "background_producer_incomplete_exit:CancelledError:server_shutdown",
])
async def test_system_cancellation_preserves_visible_failure_bubble(monkeypatch, reason):
    sid, eid, pid = uuid4(), uuid4(), uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "status": "running", "owner_instance": svc._EXECUTION_OWNER_INSTANCE,
        "owner_epoch": 2, "lease_valid": True, "completed_at": None,
    }
    conn.fetchval.side_effect = lambda query, *args: eid if "RETURNING id" in query else None
    monkeypatch.setattr(svc, "_schedule_interrupted_auto_resume", AsyncMock(return_value=False))
    await svc._mark_execution_interrupted(
        conn, str(sid), str(eid), reason, placeholder_id=str(pid), expected_owner_epoch=2,
    )
    queries = [call.args[0] for call in conn.execute.await_args_list]
    assert not any("DELETE FROM chat_messages" in query for query in queries)
    assert any("intent = 'interruption_notice'" in query and "is_hidden = FALSE" in query for query in queries)
    conn.fetchrow.return_value["status"] = "interrupted"
    writes_before_late_callback = conn.execute.await_count
    await svc._mark_execution_interrupted(
        conn, str(sid), str(eid), reason, placeholder_id=str(pid), expected_owner_epoch=2,
    )
    assert conn.execute.await_count == writes_before_late_callback
    assert not any("DELETE FROM chat_messages" in call.args[0] for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_superseded_partial_remains_visible_after_history_refresh(monkeypatch):
    sid, eid, pid = uuid4(), uuid4(), uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "status": "running", "owner_instance": svc._EXECUTION_OWNER_INSTANCE,
        "owner_epoch": 2, "lease_valid": True, "completed_at": None,
    }
    conn.fetchval.side_effect = lambda query, *args: eid if "RETURNING id" in query else None
    monkeypatch.setattr(svc, "_schedule_interrupted_auto_resume", AsyncMock(return_value=False))

    await svc._mark_execution_interrupted(
        conn, str(sid), str(eid), "superseded while preserving partial response",
        partial_content="조사 결과를 설명합니다.", placeholder_id=str(pid), expected_owner_epoch=2,
    )

    partial_update = next(
        call for call in conn.execute.await_args_list
        if "UPDATE chat_messages" in call.args[0] and "intent = $3" in call.args[0]
    )
    assert partial_update.args[3] == "_archived_partial"
    assert "is_hidden = FALSE" in partial_update.args[0]
    assert "조사 결과를 설명합니다." in partial_update.args[1]


@pytest.mark.asyncio
async def test_competing_placeholder_archives_text_without_exposing_progress_only():
    sid, eid = uuid4(), uuid4()
    answer_id, spinner_id = uuid4(), uuid4()
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"id": answer_id, "content": "⏳ _AI가 응답을 생성 중입니다..._\n실제 답변"},
        {"id": spinner_id, "content": "⏳ _AI가 응답을 생성 중입니다..._"},
    ]

    await svc._archive_competing_stream_placeholder(conn, sid, eid)

    updates = conn.execute.await_args_list
    assert updates[0].args[2:] == ("실제 답변", False)
    assert updates[1].args[3] is True


@pytest.mark.asyncio
async def test_superseded_resume_is_refused_even_with_budget_reset(monkeypatch):
    from app.core import db_pool
    from app.routers import chat

    sid, eid = uuid4(), uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = {"execution_id": str(eid)}

    @asynccontextmanager
    async def acquire():
        yield conn

    monkeypatch.setattr(db_pool, "get_pool", lambda: SimpleNamespace(acquire=acquire))
    monkeypatch.setattr(svc, "get_session", AsyncMock(return_value={"id": sid}))
    monkeypatch.setattr(svc, "get_streaming_status", lambda sid: None)
    monkeypatch.setattr(svc, "_is_local_active_api_slot", lambda: True)
    monkeypatch.setattr(svc, "_active_bg_tasks", {})
    monkeypatch.setattr(svc, "_execution_has_newer_user_message", AsyncMock(return_value=True))
    claim = AsyncMock()
    monkeypatch.setattr(svc, "_claim_execution_lease", claim)
    monkeypatch.setattr(chat, "_tenant_id", lambda context: str(uuid4()))

    result = await chat.resume_interrupted(
        sid, chat.ResumeInterruptedRequest(reset_retry_count=True), context=None,
    )
    assert result["code"] == "chat_resume_superseded"
    claim.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_foreign_lease_blocks_recovery_before_placeholder_mutation(monkeypatch):
    from app.routers import chat

    conn = AsyncMock()
    conn.fetchrow.return_value = {"retry_count": 0, "last_user_msg": "question"}
    monkeypatch.setattr(svc, "_is_local_active_api_slot", lambda: True)
    monkeypatch.setattr(svc, "_active_bg_tasks", {})
    monkeypatch.setattr(svc, "_execution_has_newer_user_message", AsyncMock(return_value=False))
    monkeypatch.setattr(svc, "_claim_execution_lease", AsyncMock(return_value=None))
    assert not await chat._schedule_recovery_auto_resume(conn, uuid4(), uuid4(), uuid4(), "partial")
    conn.execute.assert_not_awaited()
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("slot,slot_health,expected_stream_calls", [
    ("2", {"auth_available": False, "status": "missing_access_token"}, 0),
    ("1", {"auth_available": True, "healthy": False, "status": "validation_stale"}, 1),
    ("1", {"auth_available": True, "refresh_capable": True, "expired": True}, 1),
    ("4", None, 1),
])
async def test_slot_preflight_only_rejects_proven_auth_unavailability(monkeypatch, slot, slot_health, expected_stream_calls):
    from app.services import model_selector as models

    health = {"claude_model_contract": {"version": models.CONTRACT_VERSION}, "slot_auth": {}}
    if slot_health is not None:
        health["slot_auth"][slot] = slot_health
    client = SimpleNamespace()
    client.get = AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: health))

    @asynccontextmanager
    async def stream(*args, **kwargs):
        yield SimpleNamespace(status_code=503, aread=AsyncMock(return_value=b"test end"))

    client.stream = Mock(side_effect=stream)

    @asynccontextmanager
    async def factory(*args, **kwargs):
        yield client

    monkeypatch.setattr(models.httpx, "AsyncClient", factory)
    events = [event async for event in models._stream_cli_relay_once(
        "claude-opus-5", "system", [{"role": "user", "content": "ping"}], oauth_slot=slot,
    )]
    assert client.stream.call_count == expected_stream_calls
    if not expected_stream_calls:
        assert events[-1]["error_type"] == "oauth_slot_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["oauth_slot_unavailable", "oauth_slot_busy"])
async def test_unavailable_slot_immediately_returns_to_account_fallback(monkeypatch, reason):
    from app.services import model_selector as models

    calls = []

    async def failed_once(*args, **kwargs):
        calls.append(kwargs)
        yield {"type": "error", "content": reason}

    monkeypatch.setattr(models, "_stream_cli_relay_once", failed_once)
    monkeypatch.setattr(models, "_byok_owner_context", AsyncMock(return_value={"exempt": True}))
    events = [event async for event in models._stream_cli_relay(
        "claude-opus-5", "system", [{"role": "user", "content": "ping"}], oauth_slot="2",
    )]
    assert len(calls) == 1
    assert events[-1]["type"] == "error"
    assert models._is_cli_auth_error(reason)  # Also suppresses stale SDK fallback.
