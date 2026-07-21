"""PC Agent WebSocket disconnect 회귀 테스트."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, call

import pytest
from fastapi import WebSocketDisconnect

from app.api import pc_agent


class _DummyTask:
    def cancel(self) -> None:
        return None


class _DummyRequest:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


class _TimeoutWebSocket:
    def __init__(self) -> None:
        self.close_calls: list[tuple[int, str]] = []
        self._messages = [
            {
                "type": "register",
                "id": "register-1",
                "payload": {"hostname": "ceo-pc", "os_info": "linux"},
            }
        ]

    async def accept(self) -> None:
        return None

    async def receive_json(self) -> dict[str, object]:
        if self._messages:
            return self._messages.pop(0)
        raise asyncio.TimeoutError()

    async def send_json(self, _payload: dict[str, object]) -> None:
        return None

    async def close(self, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))


class _PingFailureWebSocket:
    def __init__(self, sleep_fn) -> None:
        self.close_calls: list[tuple[int, str]] = []
        self._messages = [
            {
                "type": "register",
                "id": "register-1",
                "payload": {"hostname": "ceo-pc", "os_info": "linux"},
            }
        ]
        self._sleep = sleep_fn
        self._closed = False
        self._close_code = 1011
        self._close_reason = "server_ping_failed"

    async def accept(self) -> None:
        return None

    async def receive_json(self) -> dict[str, object]:
        if self._messages:
            return self._messages.pop(0)
        while not self._closed:
            await self._sleep(0)
        raise WebSocketDisconnect(code=self._close_code, reason=self._close_reason)

    async def send_json(self, payload: dict[str, object]) -> None:
        if payload.get("type") == "heartbeat" and payload.get("id") == "":
            raise RuntimeError("ping failed")

    async def close(self, code: int, reason: str) -> None:
        self._closed = True
        self._close_code = code
        self._close_reason = reason
        self.close_calls.append((code, reason))


def _setup_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc_agent, "_agent_connections", {})
    monkeypatch.setattr(pc_agent, "_pending_reload_disconnects", [])
    monkeypatch.setattr(pc_agent, "_RELOAD_DISCONNECT_FLUSH_TASK", None)
    monkeypatch.setattr(pc_agent, "_verify_token_db", AsyncMock(return_value=True))
    monkeypatch.setattr(pc_agent.pc_agent_manager, "register_agent", Mock())
    monkeypatch.setattr(pc_agent.pc_agent_manager, "unregister_agent", Mock(return_value=True))
    monkeypatch.setattr(pc_agent.pc_agent_manager, "update_heartbeat", Mock())
    monkeypatch.setattr(pc_agent.pc_agent_manager, "receive_result", Mock())
    monkeypatch.setattr(pc_agent.pc_agent_manager, "broadcast_frame", AsyncMock())


@pytest.mark.asyncio
async def test_flush_pending_reload_disconnects_records_stale_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_record = AsyncMock()

    monkeypatch.setattr(pc_agent, "_pending_reload_disconnects", ["agent-a", "agent-b"])
    monkeypatch.setattr(pc_agent, "_RELOAD_DISCONNECT_FLUSH_TASK", None)
    monkeypatch.setattr(pc_agent, "_record_agent_event", mock_record)

    await pc_agent._flush_pending_reload_disconnects()

    assert pc_agent._pending_reload_disconnects == []
    assert mock_record.await_args_list == [
        call(
            "agent-a",
            "disconnected",
            reason="hot_reload_stale_connection",
            metadata={"reason_source": "hot_reload_guard"},
        ),
        call(
            "agent-b",
            "disconnected",
            reason="hot_reload_stale_connection",
            metadata={"reason_source": "hot_reload_guard"},
        ),
    ]


@pytest.mark.asyncio
async def test_ws_pc_agent_records_heartbeat_timeout_and_closes_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_record = AsyncMock()
    ws = _TimeoutWebSocket()

    _setup_manager(monkeypatch)
    monkeypatch.setattr(pc_agent, "_record_agent_event", mock_record)

    def _fake_create_task(coro):
        coro.close()
        return _DummyTask()

    monkeypatch.setattr(pc_agent.asyncio, "create_task", _fake_create_task)

    await pc_agent.ws_pc_agent(ws, "ceo-pc", token="token-ok")

    disconnected_calls = [
        recorded
        for recorded in mock_record.await_args_list
        if recorded.args[:2] == ("ceo-pc", "disconnected")
    ]
    connected_calls = [
        recorded
        for recorded in mock_record.await_args_list
        if recorded.args[:2] == ("ceo-pc", "connected")
    ]

    assert len(connected_calls) == 1
    assert disconnected_calls
    assert disconnected_calls[-1].kwargs["reason"] == "heartbeat_timeout"
    assert disconnected_calls[-1].kwargs["metadata"]["close_reason"] == "heartbeat_timeout"
    assert ws.close_calls[-1] == (1011, "heartbeat_timeout")


@pytest.mark.asyncio
async def test_ws_pc_agent_records_disconnect_when_server_ping_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_record = AsyncMock()
    original_sleep = asyncio.sleep
    ws = _PingFailureWebSocket(original_sleep)

    _setup_manager(monkeypatch)
    monkeypatch.setattr(pc_agent, "_record_agent_event", mock_record)

    async def _fast_sleep(_seconds: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(pc_agent.asyncio, "sleep", _fast_sleep)

    await pc_agent.ws_pc_agent(ws, "ceo-pc", token="token-ok")

    disconnected_calls = [
        recorded
        for recorded in mock_record.await_args_list
        if recorded.args[:2] == ("ceo-pc", "disconnected")
    ]

    assert disconnected_calls
    assert disconnected_calls[0].kwargs["reason"] == "server_ping_failed"
    assert disconnected_calls[0].kwargs["metadata"]["reason_source"] == "server_ping"
    assert ws.close_calls[-1] == (1011, "server_ping_failed")


@pytest.mark.asyncio
async def test_pc_agent_status_uses_peer_fallback_when_local_backend_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pc_agent, "_flush_pending_reload_disconnects", AsyncMock())
    monkeypatch.setattr(pc_agent.pc_agent_manager, "list_agent_statuses", Mock(return_value=[]))
    monkeypatch.setattr(
        pc_agent,
        "_request_peer_fallback_json",
        AsyncMock(
            return_value={
                "status": "online",
                "online_count": 1,
                "agents": [
                    {
                        "agent_id": "peer-pc",
                        "status": "online",
                        "heartbeat_age_seconds": 1.2,
                        "capabilities": ["pc_control"],
                        "command_types": ["shell", "cmd", "powershell"],
                        "last_seen": "2026-06-15T00:00:00Z",
                        "reconnect_guidance": "WebSocket heartbeat healthy.",
                    }
                ],
                "backend_source": "peer",
            }
        ),
    )

    result = await pc_agent.pc_agent_status(_DummyRequest())

    assert result["backend_source"] == "peer"
    assert result["agents"][0]["agent_id"] == "peer-pc"


@pytest.mark.asyncio
async def test_route_execute_uses_peer_fallback_on_local_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pc_agent.pc_agent_manager,
        "execute_routed_command",
        AsyncMock(return_value={"status": "error", "error_code": "PC_AGENT_OFFLINE", "message": "no online PC agent"}),
    )
    monkeypatch.setattr(
        pc_agent,
        "_request_peer_fallback_json",
        AsyncMock(
            return_value={
                "status": "success",
                "command_id": "peer-cmd-1",
                "result": {"status": "success", "result": {"ok": True}},
                "backend_source": "peer",
            }
        ),
    )

    request = pc_agent.RoutedCommandRequest(command_type="system_info", params={})
    result = await pc_agent.route_execute_command(request, _DummyRequest())

    assert result["status"] == "success"
    assert result["command_id"] == "peer-cmd-1"
    assert result["backend_source"] == "peer"
