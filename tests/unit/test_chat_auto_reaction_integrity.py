from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

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


def test_current_system_trigger_is_reinserted_once_as_last_user_turn():
    message_id = uuid.uuid4()
    history = [
        {"id": str(uuid.uuid4()), "role": "user", "content": "old question"},
        {"id": str(uuid.uuid4()), "role": "assistant", "content": "old answer"},
    ]

    bound = chat_service._bind_current_turn_to_history(
        history,
        user_message_id=message_id,
        content="목표 등록 후 PRD와 마일스톤을 작성하고 진행해",
    )
    status = chat_service._current_turn_binding_status(bound, message_id)

    assert bound[-1] == {
        "id": str(message_id),
        "role": "user",
        "content": "목표 등록 후 PRD와 마일스톤을 작성하고 진행해",
    }
    assert status == {
        "current_user_included": True,
        "current_user_occurrences": 1,
        "current_user_is_last": True,
    }


def test_current_turn_binding_deduplicates_and_moves_existing_row_to_end():
    message_id = uuid.uuid4()
    bound = chat_service._bind_current_turn_to_history(
        [
            {"id": str(message_id), "role": "user", "content": "persisted safe content"},
            {"id": str(uuid.uuid4()), "role": "assistant", "content": "stale answer"},
            {"id": str(message_id), "role": "user", "content": "duplicate"},
        ],
        user_message_id=message_id,
        content="exact current instruction",
    )

    assert sum(item.get("id") == str(message_id) for item in bound) == 1
    assert bound[-1]["content"] == "exact current instruction"


def test_current_turn_is_verified_in_final_llm_messages():
    messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "exact current instruction"}],
        },
    ]

    assert chat_service._current_turn_llm_binding_status(
        messages,
        "exact current instruction",
    ) == {
        "current_user_in_llm": True,
        "current_user_is_last_llm": True,
    }


def test_task_local_epoch_cannot_be_replaced_by_global_reclaim_epoch():
    execution_id = uuid.uuid4()
    id_token = chat_service._current_execution_id.set(str(execution_id))
    epoch_token = chat_service._current_execution_owner_epoch.set(1)
    chat_service._execution_owner_epochs[str(execution_id)] = 2
    try:
        assert chat_service._expected_execution_write_epoch(execution_id) == 1
        assert chat_service._expected_execution_write_epoch(execution_id, 7) == 7
    finally:
        chat_service._current_execution_owner_epoch.reset(epoch_token)
        chat_service._current_execution_id.reset(id_token)
        chat_service._execution_owner_epochs.pop(str(execution_id), None)


@pytest.mark.asyncio
async def test_interim_save_fences_old_producer_instead_of_adopting_new_epoch():
    session_id = str(uuid.uuid4())
    execution_id = uuid.uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "status": "running",
        "completed_at": None,
        "owner_instance": chat_service._EXECUTION_OWNER_INSTANCE,
        "owner_epoch": 2,
        "lease_expires_at": object(),
    })
    state = {
        "content": "old generation output",
        "tool_count": 0,
        "last_tool": "",
        "tool_events": [],
        "execution_id": str(execution_id),
        "owner_epoch": 1,
    }

    with (
        patch("app.services.chat_service.get_pool", return_value=_Pool(conn)),
        patch("app.services.chat_service._heartbeat_execution_lease", new=AsyncMock()) as heartbeat,
        patch("app.services.chat_service._archive_competing_stream_placeholder", new=AsyncMock()) as archive,
    ):
        await chat_service._interim_save_streaming(session_id, state, force=True)

    assert state["_terminal_execution_closed"] is True
    assert state["_producer_incomplete_exit"] == "execution_lease_epoch_mismatch"
    assert state["owner_epoch"] == 1
    heartbeat.assert_not_awaited()
    archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_auto_reaction_uses_background_completion_wrapper():
    session_id = str(uuid.uuid4())
    execution_id = str(uuid.uuid4())
    calls = []

    async def fake_send_message_stream(**kwargs):
        calls.append(("send", kwargs))
        yield "data: {}\n\n"

    async def fake_wrapper(stream, wrapped_session_id):
        calls.append(("wrapper", wrapped_session_id))
        async for item in stream:
            yield item
        chat_service._streaming_state[session_id] = {"execution_id": execution_id}

    with (
        patch("app.services.chat_service.send_message_stream", new=fake_send_message_stream),
        patch("app.services.chat_service.with_background_completion", new=fake_wrapper),
    ):
        result = await chat_service._consume_internal_reaction_stream(
            session_id,
            "PRD를 작성하고 배포까지 진행해",
        )

    assert result == execution_id
    assert calls[0][0] == "wrapper"
    assert calls[1][0] == "send"
    assert calls[1][1]["intent_override"] == "auto_reaction"
    chat_service._streaming_state.pop(session_id, None)


def test_ohvis_completion_alignment_rejects_unrelated_old_answer():
    goal_id = "0a173409-0917-4c17-9c17-000000000002"
    instruction = f"목표 {goal_id} 등록 후 PRD를 저장하고 마일스톤을 진행해"

    wrong = chat_service._assess_auto_reaction_alignment(
        instruction,
        "브라우저 테스트에서 새 탭이 반복 생성된 원인을 수정했습니다.",
    )
    correct = chat_service._assess_auto_reaction_alignment(
        instruction,
        f"목표 {goal_id}에 PRD를 등록했고 마일스톤 11개를 생성했습니다.",
    )

    assert wrong["aligned"] is False
    assert correct["aligned"] is True
    assert goal_id in correct["matched_identifiers"]
