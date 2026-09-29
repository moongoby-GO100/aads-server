"""Exercise authenticated registration and stale socket observation boundaries."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.api import pc_agent as api
from app.services.pc_agent_manager import PCAgentManager


class Socket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.close_calls = []

    async def accept(self):
        pass

    async def receive_json(self):
        message = next(self.messages, asyncio.TimeoutError())
        if callable(message):
            message = message()
        if isinstance(message, Exception):
            raise message
        return message

    async def send_json(self, payload):
        pass

    async def close(self, **kwargs):
        self.close_calls.append(kwargs)


@pytest.fixture
def context(monkeypatch):
    manager = PCAgentManager()
    events = AsyncMock()
    monkeypatch.setattr(api, "pc_agent_manager", manager)
    monkeypatch.setattr(api, "_record_agent_event", events)
    monkeypatch.setattr(api, "_flush_pending_reload_disconnects", AsyncMock())
    monkeypatch.setattr(api, "_notify_chat_session_disconnect", AsyncMock())
    monkeypatch.setattr(api, "_verify_token_db", AsyncMock(return_value=(True, "alice", "tenant-a")))
    monkeypatch.setattr(api, "_agent_connections", {})
    monkeypatch.setattr(api, "_agent_connect_locks", {})
    return manager, events


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [{"type": "heartbeat", "id": "", "payload": {}}, asyncio.TimeoutError(), ValueError("bad json")])
async def test_registration_failures_share_authenticated_connection(context, first):
    manager, events = context
    await api.ws_pc_agent(Socket([first]), "pc", token="verified-token")
    calls = events.await_args_list
    assert [call.args[1] for call in calls] == ["socket_connected", "register_failed"]
    assert calls[0].kwargs["metadata"]["connection_id"] == calls[1].kwargs["metadata"]["connection_id"]
    assert calls[1].kwargs["metadata"]["user_id"] == "alice"
    assert calls[1].kwargs["metadata"]["tenant_id"] == "tenant-a"
    assert manager.get_agent("pc") is None


@pytest.mark.asyncio
async def test_stale_socket_cannot_update_replacement_or_copy_its_observation(context):
    manager, events = context
    replacement = Socket([])
    def replace():
        manager.register_agent("pc", replacement, {}, owner_user_id="bob", owner_tenant_id="tenant-b")
        manager.update_observation("pc", {"cpu_percent": 14})
        return {"type": "heartbeat", "id": "", "payload": {"resources": {"cpu_percent": 99}}}
    original = Socket([{"type": "register", "id": "", "payload": {}}, replace])
    await api.ws_pc_agent(original, "pc", token="verified-token")
    assert manager.is_current_connection("pc", replacement)
    assert manager.get_last_observation("pc")["cpu_percent"] == 14
    last = events.await_args_list[-1]
    assert last.args[1] == "disconnected"
    assert last.kwargs["reason"] == "replaced_by_new_connection"
    assert "last_observation" not in last.kwargs["metadata"]


@pytest.mark.asyncio
async def test_unexpected_error_retains_last_contact_observation(context):
    manager, events = context
    ws = Socket([
        {"type": "register", "id": "", "payload": {}},
        {"type": "heartbeat", "id": "", "payload": {"resources": {"cpu_percent": 67}}},
        ValueError("broken frame"),
    ])
    await api.ws_pc_agent(ws, "pc", token="verified-token")
    last = events.await_args_list[-1]
    assert last.kwargs["reason"] == "unexpected_error"
    assert last.kwargs["metadata"]["last_observation"]["cpu_percent"] == 67
    assert last.kwargs["metadata"]["last_observation"]["classification"] == "contact_lost"
    ids = {call.kwargs["metadata"]["connection_id"] for call in events.await_args_list}
    assert len(ids) == 1
    assert any(call.args[1] == "connected" for call in events.await_args_list)


@pytest.mark.asyncio
async def test_late_registration_cannot_replace_newer_authenticated_socket(context):
    manager, events = context
    replacement = Socket([])
    def replace_before_register():
        manager.register_agent("pc", replacement, {}, owner_user_id="bob", owner_tenant_id="tenant-b")
        api._agent_connections["pc"] = replacement
        return {"type": "register", "id": "", "payload": {"user_id": "alice"}}
    await api.ws_pc_agent(Socket([replace_before_register]), "pc", token="verified-token")
    assert manager.is_current_connection("pc", replacement)
    assert manager.get_agent("pc").user_id == "bob"
    last = events.await_args_list[-1]
    assert last.args[1] == "register_failed"
    assert last.kwargs["reason"] == "replaced_by_new_connection"
