"""The durable command row must record which turn consumed the command.

2026-09-15 실측: 24시간 인터럽트 커맨드 46건이 전부 ``execution_id`` ·
``generation_id`` NULL 이었다. 지시는 반영됐지만 "어느 응답이 먹었나"를 DB 로
되물을 수 없었고, 채팅 화면이 응답 버블에 반영 표시를 못 한 근본 원인이 그것
이다.  여기서 잠그는 계약은 하나다 — 핸들러가 대상 실행/세대를 알려주면
``complete_command`` / ``fail_command`` 가 그것을 그대로 받는다.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")

from app.models.chat import ChatCommandRequest
from app.routers import chat as chat_router

EXECUTION_ID = "44444444-4444-4444-8444-444444444444"
GENERATION_ID = "55555555-5555-4555-8555-555555555555"


class _Record:
    def __init__(self, command_id, session_id, status="accepted"):
        self.command_id = command_id
        self.session_id = session_id
        self.status = status

    def to_payload(self, *, replayed: bool = False) -> dict:
        return {
            "command_id": str(self.command_id),
            "session_id": str(self.session_id),
            "status": self.status,
            "replayed": replayed,
        }


async def _submit(handler_result: dict, *, command_type: str = "interrupt"):
    """Drive ``submit_chat_command`` with every durable boundary mocked."""
    session_id = uuid4()
    tenant_id = uuid4()
    record = _Record(uuid4(), session_id)

    complete = AsyncMock(return_value=_Record(record.command_id, session_id, "succeeded"))
    fail = AsyncMock(return_value=_Record(record.command_id, session_id, "failed"))

    handler_name = {
        "interrupt": "interrupt_session",
        "stop": "stop_session_streaming",
        "resume": "resume_interrupted",
    }[command_type]

    with (
        patch.object(
            chat_router.svc, "get_session", AsyncMock(return_value={"id": str(session_id)})
        ),
        patch("app.services.chat_commands.begin_command", AsyncMock(return_value=(record, False))),
        patch("app.services.chat_commands.mark_command_running", AsyncMock(return_value=record)),
        patch("app.services.chat_commands.complete_command", complete),
        patch("app.services.chat_commands.fail_command", fail),
        patch.object(chat_router, handler_name, AsyncMock(return_value=handler_result)),
    ):
        await chat_router.submit_chat_command(
            session_id=session_id,
            body=ChatCommandRequest(command_type=command_type, payload={"content": "추가 지시"}),
            idempotency_key="unit-test-key",
            context={"tenant": {"id": str(tenant_id)}},
        )

    return complete, fail


@pytest.mark.asyncio
async def test_succeeded_interrupt_stamps_execution_and_generation():
    complete, fail = await _submit(
        {
            "queued": True,
            "message_id": str(uuid4()),
            "execution_id": EXECUTION_ID,
            "generation_id": GENERATION_ID,
        }
    )

    fail.assert_not_called()
    complete.assert_awaited_once()
    kwargs = complete.await_args.kwargs
    assert kwargs["execution_id"] == UUID(EXECUTION_ID)
    assert kwargs["generation_id"] == UUID(GENERATION_ID)


@pytest.mark.asyncio
async def test_missing_ids_settle_as_null_instead_of_raising():
    """A handler that cannot name a turn must still settle its command.

    ``stop`` has no generation to point at; treating that as an error would
    turn a bookkeeping gap into a failed command.
    """
    complete, fail = await _submit({"stopped": True}, command_type="stop")

    fail.assert_not_called()
    kwargs = complete.await_args.kwargs
    assert kwargs["execution_id"] is None
    assert kwargs["generation_id"] is None


@pytest.mark.asyncio
async def test_malformed_ids_are_dropped_not_propagated():
    """Garbage in the handler payload must not reach the UUID columns."""
    complete, _fail = await _submit(
        {"queued": True, "execution_id": "not-a-uuid", "generation_id": ""}
    )

    kwargs = complete.await_args.kwargs
    assert kwargs["execution_id"] is None
    assert kwargs["generation_id"] is None


@pytest.mark.asyncio
async def test_refused_resume_is_stamped_too():
    """A refusal is still evidence about a turn — keep the pointer."""
    complete, fail = await _submit(
        {
            "resumed": False,
            "code": "chat_resume_retry_budget_exhausted",
            "message": "재시도 예산이 소진되었습니다",
            "execution_id": EXECUTION_ID,
        },
        command_type="resume",
    )

    complete.assert_not_called()
    fail.assert_awaited_once()
    kwargs = fail.await_args.kwargs
    assert kwargs["execution_id"] == UUID(EXECUTION_ID)
    assert kwargs["generation_id"] is None
