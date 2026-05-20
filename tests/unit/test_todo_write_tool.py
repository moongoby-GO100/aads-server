from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest


def test_todo_write_is_eager_core_tool():
    from app.services.tool_registry import ToolRegistry

    registry = ToolRegistry()

    assert "todo_write" in {tool["name"] for tool in registry.get_eager_tools()}
    assert "todo_write" in {tool["name"] for tool in registry.get_tools("all")}
    assert "todo_write" in {tool["name"] for tool in registry.get_tools_for_intent("code_modify")}


@pytest.mark.asyncio
async def test_todo_write_requires_bound_session():
    from app.services.tool_executor import ToolExecutor

    raw = await ToolExecutor().execute("todo_write", {"action": "list"})
    result = json.loads(raw)

    assert result["error"] == "missing_session_id"


@pytest.mark.asyncio
async def test_todo_write_completes_current_item_and_promotes_next():
    from app.services import chat_todo_service as todo_svc
    from app.services.tool_executor import ToolExecutor, current_chat_session_id

    session_id = str(uuid.uuid4())
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    first = {
        "id": first_id,
        "session_id": uuid.UUID(session_id),
        "title": "첫 작업",
        "status": todo_svc.TODO_STATUS_IN_PROGRESS,
        "sort_order": 0,
        "source": "user_turn",
        "metadata": {},
    }
    second = {
        "id": second_id,
        "session_id": uuid.UUID(session_id),
        "title": "다음 작업",
        "status": todo_svc.TODO_STATUS_PENDING,
        "sort_order": 1,
        "source": "user_turn",
        "metadata": {},
    }
    completed = {**first, "status": todo_svc.TODO_STATUS_COMPLETED}
    promoted = {**second, "status": todo_svc.TODO_STATUS_IN_PROGRESS}

    list_items = AsyncMock(side_effect=[
        [first, second],
        [second],
    ])
    update_item = AsyncMock(side_effect=[completed, promoted])
    token = current_chat_session_id.set(session_id)
    try:
        with (
            patch("app.services.chat_todo_service.list_todo_items", list_items),
            patch("app.services.chat_todo_service.update_session_todo_item", update_item),
        ):
            result = await ToolExecutor()._todo_write({"action": "complete", "current": True})
    finally:
        current_chat_session_id.reset(token)

    assert result["item"]["status"] == todo_svc.TODO_STATUS_COMPLETED
    assert result["promoted_next"]["id"] == second_id
    assert update_item.await_args_list[0].kwargs["todo_id"] == str(first_id)
    assert update_item.await_args_list[1].kwargs["todo_id"] == str(second_id)
