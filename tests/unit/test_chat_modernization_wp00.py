"""Executable WP00 baseline for existing chat continuity contracts.

WP00 / C10-C21 / FR05-FR22 / INV03-INV13 / ADR06-ADR11 / T05-T22.
Synthetic IDs and payloads only; no DB, Redis, provider, or network connection.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from app.core import interrupt_queue
from app.services import chat_service, stream_worker

SESSION_ID = "00000000-0000-4000-8000-000000000001"
EXECUTION_ID = "00000000-0000-4000-8000-000000000002"
MESSAGE_ID = "00000000-0000-4000-8000-000000000003"


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


async def _collect(generator):
    return [item async for item in generator]


@pytest.fixture(autouse=True)
def _clean_fixed_session_state():
    interrupt_queue.set_streaming(SESSION_ID, False)
    interrupt_queue.pop_interrupts(SESSION_ID)
    interrupt_queue.pop_pending_interrupts(SESSION_ID)
    chat_service._streaming_state.pop(SESSION_ID, None)
    chat_service._active_bg_tasks.pop(SESSION_ID, None)
    yield
    interrupt_queue.set_streaming(SESSION_ID, False)
    interrupt_queue.pop_interrupts(SESSION_ID)
    interrupt_queue.pop_pending_interrupts(SESSION_ID)
    chat_service._streaming_state.pop(SESSION_ID, None)
    chat_service._active_bg_tasks.pop(SESSION_ID, None)


def test_visibility_sql_preserves_meaningful_interrupted_and_runner_replies():
    """C13/C20, FR08/FR43, INV03/INV10, T08/T43."""
    active = chat_service._visible_message_filter(is_active=True, include_streaming=True)
    history = chat_service._visible_message_filter(is_active=False, include_streaming=False)

    assert "intent IN ('runner_response', 'interrupted_partial', '_archived_partial')" in active
    assert "length(COALESCE(content, '')) > 200" in active
    assert "intent IS DISTINCT FROM 'runner_response'" not in active
    assert "intent IS DISTINCT FROM 'runner_response'" not in history
    assert "is_hidden = FALSE" in active


@pytest.mark.asyncio
async def test_interrupted_partial_retry_keeps_same_execution_and_message_identity():
    """C11/C18, FR08/FR21, INV07/INV09, T08/T21."""
    connection = AsyncMock()
    connection.fetchrow = AsyncMock(
        return_value={"id": UUID(MESSAGE_ID), "created_at_text": "2026-09-12T00:00:00Z"}
    )
    state = {"execution_id": EXECUTION_ID, "owner_epoch": 7, "completed": False}

    with (
        patch.object(chat_service, "get_pool", return_value=_Pool(connection)),
        patch.dict(chat_service._streaming_state, {SESSION_ID: state}),
    ):
        saved = await chat_service._save_interrupted_partial_message(
            SESSION_ID,
            "보존해야 하는 합성 부분 응답",
            reason="missing_done_event_stream_closed:attempt=1",
            execution_id=EXECUTION_ID,
            continuing=True,
        )

    assert saved["id"] == MESSAGE_ID
    assert saved["execution_id"] == EXECUTION_ID
    assert saved["intent"] == "streaming_placeholder"
    assert saved["model_used"] == "streaming"
    assert state["completed"] is False
    sql, *_ = connection.fetchrow.await_args.args
    assert "owner_epoch = $4" in sql
    assert "lease_expires_at > NOW()" in sql


def test_additional_instruction_queue_preserves_order_attachments_and_pending_handoff():
    """C17, FR19/FR20, INV05/INV13, ADR09, T19/T20."""
    interrupt_queue.set_streaming(SESSION_ID, True)
    interrupt_queue.push_interrupt(SESSION_ID, "첫 추가 지시")
    interrupt_queue.push_interrupt(
        SESSION_ID,
        "두 번째 추가 지시",
        [{"type": "text", "name": "synthetic.txt"}],
    )
    interrupt_queue.set_streaming(SESSION_ID, False)

    assert interrupt_queue.pop_pending_interrupts(SESSION_ID) == [
        {"content": "첫 추가 지시", "attachments": []},
        {
            "content": "두 번째 추가 지시",
            "attachments": [{"type": "text", "name": "synthetic.txt"}],
        },
    ]


@pytest.mark.asyncio
async def test_stop_without_live_task_does_not_discard_queued_instruction():
    """C17/C21, FR19-FR21, INV05/INV07/INV13, T19-T21."""
    interrupt_queue.set_streaming(SESSION_ID, True)
    interrupt_queue.push_interrupt(SESSION_ID, "중지 뒤에도 보존할 지시")

    result = await chat_service.stop_session_streaming(SESSION_ID)

    assert result["stopped"] is False
    assert interrupt_queue.is_streaming(SESSION_ID) is False
    assert interrupt_queue.pop_pending_interrupts(SESSION_ID) == [
        {"content": "중지 뒤에도 보존할 지시", "attachments": []}
    ]


def test_resume_requires_done_and_separates_fencing_from_retryable_transport_errors():
    """C05/C18/C19, FR05/FR21, INV04/INV07, T05/T21."""
    with pytest.raises(RuntimeError, match="resume_stream_missing_done_event"):
        chat_service._require_resume_done_event(False, 100_000)
    assert chat_service._require_resume_done_event(True, 0) is None
    assert chat_service._is_resume_retryable(
        RuntimeError("resume_stream_missing_done_event")
    )
    assert not chat_service._is_resume_retryable(chat_service.ResumeFencedOut("lost lease"))


@pytest.mark.asyncio
async def test_redis_resume_replay_keeps_event_id_and_distinct_done_signal():
    """C10/C19, FR05-FR07, INV04-INV06, ADR06/ADR09, T05-T07."""
    cached = [
        {
            "id": "10-1",
            "data": 'data: {"type":"delta","content":"한글 👩🏽‍💻"}\n\n',
        },
        {"id": "10-2", "done": True},
    ]
    with patch.object(stream_worker._rs, "read_tokens_after", new=AsyncMock(return_value=cached)):
        events = await _collect(stream_worker.deliver_sse(EXECUTION_ID, "0", timeout_sec=1))

    assert events == [
        'id:10-1\ndata: {"type":"delta","content":"한글 👩🏽‍💻"}\n\n',
        f'data: {json.dumps({"type": "resume_done"})}\n\n',
    ]


@pytest.mark.asyncio
async def test_missing_redis_stream_is_unavailable_not_completed():
    """C19, FR05/FR07, INV04/INV06, T05/T07."""
    with (
        patch.object(stream_worker._rs, "read_tokens_after", new=AsyncMock(return_value=[])),
        patch.object(stream_worker._rs, "xread_blocking", new=AsyncMock(return_value=[])),
        patch.object(stream_worker._rs, "get_stream_info", new=AsyncMock(return_value=None)),
    ):
        events = await _collect(stream_worker.deliver_sse(EXECUTION_ID, "10-1", timeout_sec=1))

    assert any('"type": "resume_unavailable"' in event for event in events)
    assert not any('"type": "resume_done"' in event for event in events)
