"""작업 중 /chat/messages/send 로 온 지시도 추가 지시로 DB 에 접수돼야 한다.

2026-10-29 세션 65f926cb 08:35: 시스템 다음 단계 턴이 돌던 중 대표님 지시가
send 경로로 들어왔고, 서버는 push_interrupt 만 해 원문을 저장하지 않았다.
새로고침하자 지시 버블이 사라졌고 원문은 복구할 수 없었다(72시간 7건).
"""

from __future__ import annotations

import inspect
import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")

from app.routers import chat as chat_router
from tests.unit.test_chat_interrupt_receipt_failclose import (
    MESSAGE_ID,
    _Pool,
    _Transaction,
    _live_connection,
)


async def _send_as_interrupt(connection, **overrides):
    session_id = str(uuid4())
    connection.transaction = MagicMock(return_value=_Transaction())
    kwargs = {
        "content": "로그인 방식 분류하고 부족한 지원 점검해",
        "attachments": [],
        "idempotency_key": "web-send-1",
        "client_env": None,
        "context": {"tenant": {"id": str(uuid4())}},
    }
    kwargs.update(overrides)
    with (
        patch("app.core.db_pool.get_pool", return_value=_Pool(connection)),
        patch.object(chat_router.svc, "get_session", AsyncMock(return_value={"id": session_id})),
        patch.object(chat_router, "is_streaming", return_value=True),
        patch.object(chat_router, "set_streaming", MagicMock()),
        patch.object(chat_router, "push_interrupt", MagicMock()) as push_interrupt,
    ):
        result = await chat_router._queue_send_as_interrupt(session_id, **kwargs)
    return result, push_interrupt, session_id


def _inserted_user_messages(connection) -> list:
    return [c for c in connection.fetchval.call_args_list if "INSERT INTO chat_messages" in str(c.args[0])]


@pytest.mark.asyncio
async def test_send_during_running_turn_saves_the_instruction_before_queueing():
    connection = _live_connection()
    connection.execute.side_effect = ["UPDATE 1"]

    result, push_interrupt, session_id = await _send_as_interrupt(connection)

    assert result is not None
    assert result["status"] == "interrupt_queued"  # 기존 응답 필드 유지
    assert result["session_id"] == session_id
    assert result["message_id"] == MESSAGE_ID
    assert len(_inserted_user_messages(connection)) == 1
    assert any("INSERT INTO chat_interrupt_states" in str(c.args[0]) for c in connection.fetchrow.call_args_list)
    push_interrupt.assert_called_once()
    assert push_interrupt.call_args.kwargs["message_id"] == MESSAGE_ID


@pytest.mark.asyncio
async def test_resend_with_same_key_returns_the_existing_receipt():
    connection = _live_connection()
    with patch.object(chat_router._ilc, "find_by_idempotency_key", AsyncMock(return_value=MESSAGE_ID)), \
            patch.object(chat_router._ilc, "get_receipt", AsyncMock(return_value={"state": "QUEUED"})):
        result, push_interrupt, _ = await _send_as_interrupt(connection)

    assert result["message_id"] == MESSAGE_ID
    assert result["duplicate"] is True
    assert _inserted_user_messages(connection) == []
    push_interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_stale_turn_is_not_queued_so_send_falls_back_to_a_normal_turn():
    connection = _live_connection(superseded=True)

    result, push_interrupt, _ = await _send_as_interrupt(connection)

    assert result is None
    assert _inserted_user_messages(connection) == []
    push_interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_overlong_idempotency_key_does_not_reject_the_instruction():
    connection = _live_connection()
    connection.execute.side_effect = ["UPDATE 1"]

    result, _, _ = await _send_as_interrupt(connection, idempotency_key="k" * 200)

    assert result is not None
    assert result["idempotency_key"] is None


def test_send_message_no_longer_queues_without_a_receipt():
    source = inspect.getsource(chat_router.send_message)
    assert "_queue_send_as_interrupt(" in source
    assert "push_interrupt(" not in source
