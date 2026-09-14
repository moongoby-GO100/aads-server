"""WP01 regression tests for durable additional-instruction receipts.

2026-09-15: the receipt INSERT now returns the message id and the acknowledgement
carries the execution/generation it targets, so the durable command row can say
*which answer* consumed the instruction.  The fail-closed contract below is
unchanged — a receipt that does not commit must never be acknowledged.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")

from app.routers import chat as chat_router


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


class _Transaction:
    def __init__(self):
        self.exit_exception_type = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exception_type, _exception, _traceback):
        self.exit_exception_type = exception_type
        return False


EXECUTION_ID = "11111111-1111-4111-8111-111111111111"
GENERATION_ID = "22222222-2222-4222-8222-222222222222"
MESSAGE_ID = "33333333-3333-4333-8333-333333333333"


def _live_execution_row() -> dict[str, object]:
    return {
        "execution_id": EXECUTION_ID,
        "status": "running",
        "last_event_id": "123-0",
        "updated_age_seconds": 3,
        "started_age_seconds": 25,
        "partial_content": "조사 결과를 정리하는 중입니다.",
        "tools_called": [],
    }


def _fetchval_router(*, superseded: bool = False, insert_error: Exception | None = None):
    """Route the three fetchval call sites by their SQL.

    ``interrupt_session`` asks fetchval three different questions now — the
    supersede probe, the receipt INSERT ... RETURNING id, and the active
    generation lookup.  A single ``return_value`` would answer all three the
    same way and hide which one was actually exercised.
    """

    def _answer(sql, *_args, **_kwargs):
        text = str(sql)
        if "INSERT INTO chat_messages" in text:
            if insert_error is not None:
                raise insert_error
            return MESSAGE_ID
        if "chat_execution_generations" in text:
            return GENERATION_ID
        return superseded

    return _answer


def _live_connection(*, superseded: bool = False, insert_error: Exception | None = None) -> AsyncMock:
    """A connection whose session has exactly one live execution.

    ``interrupt_session`` also fails closed when a newer execution has already
    superseded the running one, so the supersede probe has to answer False for
    the turn to count as the session's current turn.
    """
    connection = AsyncMock()
    connection.fetchrow.return_value = _live_execution_row()
    connection.fetchval.side_effect = _fetchval_router(
        superseded=superseded, insert_error=insert_error
    )
    return connection


async def _call_interrupt(connection):
    session_id = uuid4()
    request = chat_router.InterruptRequest(
        content="추가 지시 반영해 주세요",
        attachments=[],
    )
    context = {"tenant": {"id": str(uuid4())}}
    transaction = _Transaction()
    connection.transaction = MagicMock(return_value=transaction)

    with (
        patch("app.core.db_pool.get_pool", return_value=_Pool(connection)),
        patch.object(
            chat_router.svc,
            "get_session",
            AsyncMock(return_value={"id": str(session_id)}),
        ),
        patch.object(chat_router, "is_streaming", return_value=True),
        patch.object(chat_router, "set_streaming", MagicMock()),
        patch.object(chat_router, "push_interrupt", MagicMock()) as push_interrupt,
    ):
        try:
            result = await chat_router.interrupt_session(session_id, request, context)
            error = None
        except HTTPException as exc:
            result = None
            error = exc

    return result, error, push_interrupt, transaction, session_id


def _assert_fail_closed(error: HTTPException | None, push_interrupt: MagicMock) -> None:
    assert error is not None
    assert error.status_code == 503
    assert error.detail == {
        "code": "interrupt_receipt_failed",
        "message": (
            "추가 지시를 저장하지 못해 접수하지 않았습니다. "
            "입력 내용을 확인한 뒤 다시 시도해 주세요."
        ),
        "queued": False,
    }
    push_interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_superseded_execution_is_rejected_instead_of_queued():
    """A newer execution means the running turn is no longer the current one."""
    connection = _live_connection(superseded=True)
    connection.execute.side_effect = ["UPDATE 1"]

    result, error, push_interrupt, _transaction, _session_id = await _call_interrupt(connection)

    assert error is None
    assert result is not None
    assert result["queued"] is False
    assert "superseded=True" in result["reason"]
    push_interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_insert_failure_is_not_reported_or_enqueued():
    connection = _live_connection(insert_error=RuntimeError("synthetic insert failure"))
    connection.execute.side_effect = ["UPDATE 1"]

    result, error, push_interrupt, transaction, _session_id = await _call_interrupt(connection)

    assert result is None
    _assert_fail_closed(error, push_interrupt)
    assert "synthetic" not in str(error.detail)
    assert transaction.exit_exception_type is RuntimeError


@pytest.mark.asyncio
async def test_counter_failure_rolls_back_receipt_and_does_not_enqueue():
    connection = _live_connection()
    connection.execute.side_effect = RuntimeError("synthetic count failure")
    result, error, push_interrupt, transaction, _session_id = await _call_interrupt(connection)

    assert result is None
    _assert_fail_closed(error, push_interrupt)
    assert transaction.exit_exception_type is RuntimeError


@pytest.mark.asyncio
async def test_missing_session_counter_update_fails_closed():
    connection = _live_connection()
    connection.execute.side_effect = ["UPDATE 0"]
    result, error, push_interrupt, transaction, _session_id = await _call_interrupt(connection)

    assert result is None
    _assert_fail_closed(error, push_interrupt)
    assert transaction.exit_exception_type is RuntimeError


@pytest.mark.asyncio
async def test_committed_receipt_is_enqueued_and_acknowledged():
    connection = _live_connection()
    connection.execute.side_effect = ["UPDATE 1"]

    result, error, push_interrupt, transaction, session_id = await _call_interrupt(connection)

    assert error is None
    assert result is not None
    assert result["queued"] is True
    push_interrupt.assert_called_once_with(
        str(session_id),
        "추가 지시 반영해 주세요",
        None,
    )
    assert transaction.exit_exception_type is None
    connection.transaction.assert_called_once_with()


@pytest.mark.asyncio
async def test_acknowledgement_carries_the_turn_it_was_attached_to():
    """The receipt must say which answer will consume it.

    Without these three ids the instruction is accepted into a void: the chat
    UI has nothing to hang a badge on, and ``chat_commands`` keeps storing NULL
    for execution_id/generation_id (46/46 on 2026-09-15).
    """
    connection = _live_connection()
    connection.execute.side_effect = ["UPDATE 1"]

    result, error, _push_interrupt, _transaction, _session_id = await _call_interrupt(connection)

    assert error is None
    assert result["message_id"] == MESSAGE_ID
    assert result["execution_id"] == EXECUTION_ID
    assert result["generation_id"] == GENERATION_ID
    # Ids must be parseable — the command wrapper casts them to UUID.
    UUID(result["execution_id"])
    UUID(result["generation_id"])


@pytest.mark.asyncio
async def test_generation_lookup_failure_still_acknowledges_the_receipt():
    """The stamp is bookkeeping; the receipt already committed.

    Losing the generation id must degrade the badge, not reject an instruction
    the user already saw accepted.
    """
    connection = _live_connection()
    connection.execute.side_effect = ["UPDATE 1"]

    def _answer(sql, *_args, **_kwargs):
        text = str(sql)
        if "INSERT INTO chat_messages" in text:
            return MESSAGE_ID
        if "chat_execution_generations" in text:
            raise RuntimeError("generation table unavailable")
        return False

    connection.fetchval.side_effect = _answer

    result, error, push_interrupt, _transaction, _session_id = await _call_interrupt(connection)

    assert error is None
    assert result["queued"] is True
    assert result["execution_id"] == EXECUTION_ID
    assert result["generation_id"] is None
    push_interrupt.assert_called_once()
