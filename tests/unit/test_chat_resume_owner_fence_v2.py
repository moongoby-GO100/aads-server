from __future__ import annotations

from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.services import chat_service


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def test_missing_done_is_never_accepted_by_length():
    with pytest.raises(RuntimeError, match="resume_stream_missing_done_event"):
        chat_service._require_resume_done_event(False, 100_000)
    assert chat_service._require_resume_done_event(True, 0) is None


def test_resume_fence_and_limit_are_not_retryable():
    assert not chat_service._is_resume_retryable(chat_service.ResumeFencedOut("lost"))
    assert not chat_service._is_resume_retryable(
        chat_service.ResumeAttemptLimitExceeded("execution_resume_attempt_limit_exceeded")
    )
    assert chat_service._is_resume_retryable(RuntimeError("resume_stream_missing_done_event"))
    assert chat_service._is_resume_retryable(RuntimeError("resume_no_meaningful_response"))


@pytest.mark.asyncio
async def test_claim_model_attempt_distinguishes_fence_before_budget():
    execution_id = uuid.uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(
        return_value={
            "retry_count": chat_service._EXECUTION_RESUME_MAX_ATTEMPTS,
            "status": "retrying",
            "owner_instance": "another-slot",
            "owner_epoch": 9,
        }
    )

    with pytest.raises(chat_service.ResumeFencedOut):
        await chat_service._claim_resume_model_attempt(conn, execution_id, owner_epoch=8)


@pytest.mark.asyncio
async def test_claim_model_attempt_reports_real_budget_exhaustion():
    execution_id = uuid.uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(
        return_value={
            "retry_count": chat_service._EXECUTION_RESUME_MAX_ATTEMPTS,
            "status": "retrying",
            "owner_instance": chat_service._EXECUTION_OWNER_INSTANCE,
            "owner_epoch": 8,
        }
    )

    with pytest.raises(chat_service.ResumeAttemptLimitExceeded):
        await chat_service._claim_resume_model_attempt(conn, execution_id, owner_epoch=8)


def test_newest_partial_never_crosses_execution_boundary():
    session_id = str(uuid.uuid4())
    chat_service._streaming_state[session_id] = {
        "execution_id": "new-execution",
        "content": "new execution content that must not leak",
    }
    try:
        assert chat_service._newest_resume_partial(
            session_id, "old-execution", "old partial"
        ) == "old partial"
        assert chat_service._newest_resume_partial(
            session_id, "new-execution", "old partial"
        ) == "new execution content that must not leak"
    finally:
        chat_service._streaming_state.pop(session_id, None)


@pytest.mark.asyncio
async def test_completed_execution_rejects_late_interrupt_before_message_mutation():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "status": "completed",
            "owner_instance": None,
            "owner_epoch": 4,
            "completed_at": object(),
            "lease_valid": False,
        }
    )

    await chat_service._mark_execution_interrupted(
        conn,
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        "resume_stream_missing_done_event",
        partial_content="late callback content",
        expected_owner_epoch=4,
    )

    conn.fetchval.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_claims_execution_before_touching_message():
    execution_id = uuid.uuid4()
    placeholder_id = uuid.uuid4()
    calls: list[str] = []

    class _Conn:
        async def fetchrow(self, query, *args):
            return {
                "status": "running",
                "owner_instance": chat_service._EXECUTION_OWNER_INSTANCE,
                "owner_epoch": 3,
                "completed_at": None,
                "lease_valid": True,
            }

        async def fetchval(self, query, *args):
            normalized = " ".join(query.split())
            calls.append(normalized)
            if "UPDATE chat_turn_executions" in normalized and "RETURNING id" in normalized:
                return execution_id
            return None

        async def execute(self, query, *args):
            calls.append(" ".join(query.split()))
            return "UPDATE 1"

    with patch(
        "app.services.chat_service._schedule_interrupted_auto_resume",
        new=AsyncMock(return_value=False),
    ):
        await chat_service._mark_execution_interrupted(
            _Conn(),
            str(uuid.uuid4()),
            str(execution_id),
            "resume_stream_missing_done_event",
            partial_content="a meaningful partial response",
            placeholder_id=str(placeholder_id),
            expected_owner_epoch=3,
        )

    terminal_index = next(
        i for i, query in enumerate(calls)
        if "UPDATE chat_turn_executions" in query and "RETURNING id" in query
    )
    message_index = next(i for i, query in enumerate(calls) if "UPDATE chat_messages" in query)
    assert terminal_index < message_index
    assert "owner_epoch = $4" in calls[terminal_index]


@pytest.mark.asyncio
async def test_direct_auto_reaction_is_durably_deferred_while_db_execution_is_live():
    enqueue = AsyncMock(return_value="deferred-id")
    with (
        patch("app.services.chat_service._is_local_active_api_slot", return_value=True),
        patch("app.services.chat_service._session_has_live_execution", new=AsyncMock(return_value=True)),
        patch("app.services.chat_service._enqueue_deferred_reaction", new=enqueue),
    ):
        result = await chat_service.trigger_ai_reaction(
            str(uuid.uuid4()),
            "[시스템] runner completed",
        )

    assert result is None
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_deferred_reaction_enqueue_dedupes_existing_pending_row():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="existing-deferred")
    with patch("app.services.chat_service.get_pool", return_value=_Pool(conn)):
        result = await chat_service._enqueue_deferred_reaction(
            str(uuid.uuid4()),
            "same message",
        )

    assert result == "existing-deferred"
    assert conn.fetchval.await_count == 1


def test_startup_resume_callbacks_forward_claimed_epoch():
    source = open("app/main.py", encoding="utf-8").read()
    callback = source.split("def _on_resume_done", 1)[1].split(
        "_resume_t.add_done_callback", 1
    )[0]
    assert "_epoch=owner_epoch" in callback
    assert callback.count("expected_owner_epoch=_epoch") == 2


def test_resume_lease_starts_before_semaphore_wait():
    source = open(chat_service.__file__, encoding="utf-8").read()
    body = source.split("async def _resume_single_stream", 1)[1].split(
        "def _cleanup_completion_ack_state", 1
    )[0]
    assert body.index("_resume_lease_task =") < body.index("async with _RESUME_SEMAPHORE")
    assert "_claim_resume_model_attempt" in body
    assert "resume_worker_fenced_out" in body
