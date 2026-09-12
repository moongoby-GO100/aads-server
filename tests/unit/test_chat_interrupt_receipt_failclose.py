"""WP01 regression tests for durable additional-instruction receipts."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

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


def _live_execution_row() -> dict[str, object]:
    return {
        "execution_id": str(uuid4()),
        "status": "running",
        "last_event_id": "123-0",
        "updated_age_seconds": 3,
        "started_age_seconds": 25,
        "partial_content": "조사 결과를 정리하는 중입니다.",
        "tools_called": [],
    }


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
async def test_insert_failure_is_not_reported_or_enqueued():
    connection = AsyncMock()
    connection.fetchrow.return_value = _live_execution_row()
    connection.execute.side_effect = RuntimeError("synthetic insert failure")

    result, error, push_interrupt, transaction, _session_id = await _call_interrupt(connection)

    assert result is None
    _assert_fail_closed(error, push_interrupt)
    assert "synthetic" not in str(error.detail)
    assert transaction.exit_exception_type is RuntimeError


@pytest.mark.asyncio
async def test_counter_failure_rolls_back_receipt_and_does_not_enqueue():
    connection = AsyncMock()
    connection.fetchrow.return_value = _live_execution_row()
    connection.execute.side_effect = ["INSERT 0 1", RuntimeError("synthetic count failure")]
    result, error, push_interrupt, transaction, _session_id = await _call_interrupt(connection)

    assert result is None
    _assert_fail_closed(error, push_interrupt)
    assert transaction.exit_exception_type is RuntimeError


@pytest.mark.asyncio
async def test_missing_session_counter_update_fails_closed():
    connection = AsyncMock()
    connection.fetchrow.return_value = _live_execution_row()
    connection.execute.side_effect = ["INSERT 0 1", "UPDATE 0"]
    result, error, push_interrupt, transaction, _session_id = await _call_interrupt(connection)

    assert result is None
    _assert_fail_closed(error, push_interrupt)
    assert transaction.exit_exception_type is RuntimeError


@pytest.mark.asyncio
async def test_committed_receipt_is_enqueued_and_acknowledged():
    connection = AsyncMock()
    connection.fetchrow.return_value = _live_execution_row()
    connection.execute.side_effect = ["INSERT 0 1", "UPDATE 1"]

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
