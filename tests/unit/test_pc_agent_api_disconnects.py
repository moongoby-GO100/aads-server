"""PC Agent WebSocket disconnect 회귀 테스트."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from fastapi import WebSocketDisconnect

from app.api import pc_agent
from app.services import session_reporter


class _DummyTask:
    def cancel(self) -> None:
        return None


class _DummyRequest:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self.cookies = {}
        self.client = None


def _internal_request() -> _DummyRequest:
    return _DummyRequest(headers={pc_agent._PEER_FALLBACK_HEADER: "1"})


def test_peer_fallback_urls_skip_local_container_and_use_peer_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AADS_CONTAINER_NAME", "aads-server")
    monkeypatch.setattr(pc_agent, "_active_api_ports", lambda: ["8100", "8102"])
    monkeypatch.setattr(pc_agent, "_active_container_name", lambda: "aads-server")

    urls = pc_agent._peer_fallback_urls("/api/v1/pc-agent/route-execute")

    assert urls[0] == "http://aads-server-green:8080/api/v1/pc-agent/route-execute"
    assert "http://aads-server:8080/api/v1/pc-agent/route-execute" not in urls
    assert all("127.0.0.1" not in url for url in urls)


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


class _HeartbeatThenTimeoutWebSocket:
    def __init__(self) -> None:
        self.close_calls: list[tuple[int, str]] = []
        self.sent: list[dict[str, object]] = []
        self._messages = [
            {
                "type": "register",
                "id": "register-1",
                "payload": {"hostname": "ceo-pc", "os_info": "Windows", "version": "1.0.71"},
            },
            {
                "type": "heartbeat",
                "id": "heartbeat-1",
                "payload": {
                    "hostname": "ceo-pc",
                    "version": "1.0.71",
                    "node_role": "interactive",
                    "agent_pid": 1234,
                    "agent_uptime_seconds": 42,
                    "agent_start_count": 3,
                    "launcher_or_parent_pid": 1200,
                    "watchdog_task": {"registered": True},
                    "startup_registration": {"registered": True},
                },
            },
        ]

    async def accept(self) -> None:
        return None

    async def receive_json(self) -> dict[str, object]:
        if self._messages:
            return self._messages.pop(0)
        raise asyncio.TimeoutError()

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)

    async def close(self, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))


def _setup_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc_agent, "_agent_connections", {})
    monkeypatch.setattr(pc_agent, "_pending_reload_disconnects", [])
    monkeypatch.setattr(pc_agent, "_RELOAD_DISCONNECT_FLUSH_TASK", None)
    monkeypatch.setattr(pc_agent, "_verify_token_db", AsyncMock(return_value=(True, "", "")))
    monkeypatch.setattr(pc_agent.pc_agent_manager, "register_agent", Mock())
    monkeypatch.setattr(pc_agent.pc_agent_manager, "unregister_agent", Mock(return_value=True))
    monkeypatch.setattr(pc_agent.pc_agent_manager, "update_heartbeat", Mock())
    monkeypatch.setattr(pc_agent.pc_agent_manager, "receive_result", Mock())
    monkeypatch.setattr(pc_agent.pc_agent_manager, "broadcast_frame", AsyncMock())


class _TokenConnection:
    def __init__(self, *, has_active_column: bool, token_row: dict[str, object] | None) -> None:
        self.has_active_column = has_active_column
        self.token_row = token_row
        self.queries: list[str] = []

    async def fetchval(self, query: str, *_args: object) -> bool:
        self.queries.append(query)
        return self.has_active_column

    async def fetchrow(self, query: str, *_args: object) -> dict[str, object] | None:
        self.queries.append(query)
        return self.token_row

    async def execute(self, query: str, *_args: object) -> str:
        self.queries.append(query)
        return "UPDATE 1"


class _TokenAcquire:
    def __init__(self, conn: _TokenConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> _TokenConnection:
        return self.conn

    async def __aexit__(self, *_args: object) -> None:
        return None


class _TokenPool:
    def __init__(self, conn: _TokenConnection) -> None:
        self.conn = conn

    def acquire(self) -> _TokenAcquire:
        return _TokenAcquire(self.conn)


def test_code_1005_is_classified_as_abnormal_close() -> None:
    classification = pc_agent._classify_disconnect_cause(
        close_code=1005,
        close_reason="",
        uptime_seconds=120.0,
        exc_type="WebSocketDisconnect",
    )

    assert classification["cause"] == "abnormal_close"
    assert classification["severity"] == "warning"


def test_server_ping_failure_has_distinct_classification() -> None:
    classification = pc_agent._classify_disconnect_cause(
        close_code=1011,
        close_reason="server_ping_failed",
        uptime_seconds=120.0,
        exc_type="RuntimeError",
    )

    assert classification["cause"] == "server_ping_failed"
    assert classification["severity"] == "warning"


def test_disconnect_alert_key_is_stable_only_inside_cooldown_window() -> None:
    first = datetime(2026, 9, 12, 11, 0, 0, tzinfo=timezone.utc)
    same_window = datetime(2026, 9, 12, 11, 4, 0, tzinfo=timezone.utc)
    next_window = datetime(2026, 9, 12, 11, 16, 0, tzinfo=timezone.utc)

    first_key = pc_agent._disconnect_alert_idempotency_key(
        "ceo-pc", "heartbeat_timeout", observed_at=first
    )
    assert first_key == pc_agent._disconnect_alert_idempotency_key(
        "ceo-pc", "heartbeat_timeout", observed_at=same_window
    )
    assert first_key != pc_agent._disconnect_alert_idempotency_key(
        "ceo-pc", "heartbeat_timeout", observed_at=next_window
    )
    assert len(first_key) <= 64


@pytest.mark.asyncio
async def test_verify_token_accepts_legacy_schema_until_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _TokenConnection(
        has_active_column=False,
        token_row={"user_id": "owner-1", "tenant_id": "tenant-1"},
    )
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _TokenPool(conn))

    valid, owner_user_id, owner_tenant_id = await pc_agent._verify_token_db("legacy-token")

    assert (valid, owner_user_id, owner_tenant_id) == (True, "owner-1", "tenant-1")
    assert all("is_active = TRUE" not in query for query in conn.queries)


@pytest.mark.asyncio
async def test_verify_token_enforces_active_state_after_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _TokenConnection(has_active_column=True, token_row=None)
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _TokenPool(conn))

    valid, owner_user_id, owner_tenant_id = await pc_agent._verify_token_db("revoked-token")

    assert (valid, owner_user_id, owner_tenant_id) == (False, "", "")
    assert any("is_active = TRUE" in query for query in conn.queries)


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
async def test_list_agents_returns_peer_snapshot_when_local_registry_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc_agent, "_latest_known_pc_agents_from_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(pc_agent.pc_agent_manager, "list_agent_statuses", Mock(return_value=[]))
    monkeypatch.setattr(
        pc_agent,
        "_request_peer_fallback_json",
        AsyncMock(
            return_value={
                "agents": [{"agent_id": "oby-ceo", "status": "online"}],
                "online_count": 1,
                "backend_source": "peer",
            }
        ),
    )

    result = await pc_agent.list_agents(_internal_request())

    assert result["agents"][0]["agent_id"] == "oby-ceo"
    assert result["online_count"] == 1
    assert result["backend_source"] == "local+peer"


@pytest.mark.asyncio
async def test_list_agents_hides_unowned_legacy_agents_for_regular_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc_agent, "_flush_pending_reload_disconnects", AsyncMock())
    monkeypatch.setattr(pc_agent, "_request_peer_fallback_json", AsyncMock(return_value=None))
    monkeypatch.setattr(pc_agent, "_latest_known_pc_agents_from_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        pc_agent.pc_agent_manager,
        "list_agent_statuses",
        Mock(
            return_value=[
                {"agent_id": "legacy-pc", "status": "online", "user_id": ""},
                {"agent_id": "owned-pc", "status": "online", "user_id": "user-1"},
                {"agent_id": "other-pc", "status": "online", "user_id": "user-2"},
            ]
        ),
    )
    monkeypatch.setattr(pc_agent, "verify_token", Mock(return_value={"sub": "user-1", "email": "user@example.com", "is_admin": False}))

    result = await pc_agent.list_agents(_DummyRequest(headers={"Authorization": "Bearer user-token"}))

    assert [agent["agent_id"] for agent in result["agents"]] == ["owned-pc"]
    assert result["online_count"] == 1


@pytest.mark.asyncio
async def test_list_agents_shows_legacy_agents_for_admin_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc_agent, "_flush_pending_reload_disconnects", AsyncMock())
    monkeypatch.setattr(pc_agent, "_request_peer_fallback_json", AsyncMock(return_value=None))
    monkeypatch.setattr(pc_agent, "_latest_known_pc_agents_from_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(pc_agent, "ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setattr(
        pc_agent.pc_agent_manager,
        "list_agent_statuses",
        Mock(
            return_value=[
                {"agent_id": "legacy-pc", "status": "online", "user_id": ""},
                {"agent_id": "owned-pc", "status": "online", "user_id": "user-1"},
            ]
        ),
    )
    monkeypatch.setattr(pc_agent, "verify_token", Mock(return_value={"sub": "admin", "email": "admin@example.com", "is_admin": True}))

    result = await pc_agent.list_agents(_DummyRequest(headers={"Authorization": "Bearer admin-token"}))

    assert [agent["agent_id"] for agent in result["agents"]] == ["legacy-pc", "owned-pc"]
    assert result["online_count"] == 2


@pytest.mark.asyncio
async def test_list_agents_appends_known_offline_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc_agent, "_flush_pending_reload_disconnects", AsyncMock())
    monkeypatch.setattr(
        pc_agent.pc_agent_manager,
        "list_agent_statuses",
        Mock(return_value=[{"agent_id": "online-pc", "status": "online", "is_online": True}]),
    )
    monkeypatch.setattr(pc_agent, "_request_peer_fallback_json", AsyncMock(return_value=None))
    monkeypatch.setattr(
        pc_agent,
        "_latest_known_pc_agents_from_events",
        AsyncMock(
            return_value=[
                {"agent_id": "online-pc", "status": "offline", "known_from_event_log": True},
                {
                    "agent_id": "offline-pc",
                    "status": "offline",
                    "is_online": False,
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "known_from_event_log": True,
                },
            ]
        ),
    )

    result = await pc_agent.list_agents(_internal_request())

    assert [agent["agent_id"] for agent in result["agents"]] == ["online-pc", "offline-pc"]
    assert result["online_count"] == 1
    assert result["offline_count"] == 1
    assert result["total_count"] == 2


def test_admin_access_allows_unowned_legacy_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc_agent.pc_agent_manager, "get_agent", Mock(return_value=SimpleNamespace(user_id="")))

    pc_agent._assert_agent_access(
        "legacy-pc",
        "admin",
        False,
        allow_unowned_legacy_agent=True,
    )


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

    assert disconnected_calls
    assert disconnected_calls[-1].kwargs["reason"] == "heartbeat_timeout"
    assert disconnected_calls[-1].kwargs["metadata"]["close_reason"] == "heartbeat_timeout"
    assert disconnected_calls[-1].kwargs["metadata"]["reason_source"] == "receive_timeout"
    assert disconnected_calls[-1].kwargs["metadata"]["receive_timeout_seconds"] == 90
    assert "last_heartbeat_age_seconds" in disconnected_calls[-1].kwargs["metadata"]
    assert ws.close_calls[-1] == (1011, "heartbeat_timeout")


@pytest.mark.asyncio
async def test_ws_pc_agent_records_runtime_heartbeat_status(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_record = AsyncMock()
    ws = _HeartbeatThenTimeoutWebSocket()

    _setup_manager(monkeypatch)
    monkeypatch.setattr(pc_agent, "_record_agent_event", mock_record)

    def _fake_create_task(coro):
        coro.close()
        return _DummyTask()

    monkeypatch.setattr(pc_agent.asyncio, "create_task", _fake_create_task)

    await pc_agent.ws_pc_agent(ws, "ceo-pc", token="token-ok")

    heartbeat_calls = [
        recorded
        for recorded in mock_record.await_args_list
        if recorded.args[:2] == ("ceo-pc", "heartbeat_status")
    ]

    assert heartbeat_calls
    metadata = heartbeat_calls[-1].kwargs["metadata"]
    assert metadata["version"] == "1.0.71"
    assert metadata["watchdog_task"]["registered"] is True


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
async def test_disconnect_notification_posts_same_session_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_report = AsyncMock(
        return_value=session_reporter.SessionReportResult(
            posted=True,
            session_id="11111111-1111-4111-8111-111111111111",
            message_id="22222222-2222-4222-8222-222222222222",
            reaction_triggered=True,
        )
    )
    monkeypatch.setattr(
        pc_agent,
        "_latest_pc_agent_alert_session_id",
        AsyncMock(return_value="11111111-1111-4111-8111-111111111111"),
    )
    monkeypatch.setattr(session_reporter, "post_session_report", post_report)

    await pc_agent._notify_chat_session_disconnect(
        agent_id="ceo-pc",
        classification={
            "cause": "heartbeat_timeout",
            "severity": "warning",
            "auto_recoverable": True,
            "uptime_seconds": 125.0,
            "close_code": 1011,
            "close_reason": "heartbeat_timeout",
            "exc_type": "TimeoutError",
        },
        metadata={
            "close_code": 1011,
            "close_reason": "heartbeat_timeout",
            "uptime_seconds": 125.0,
        },
    )

    assert post_report.await_count == 1
    kwargs = post_report.await_args.kwargs
    assert kwargs["session_id"] == "11111111-1111-4111-8111-111111111111"
    assert kwargs["source"] == "pc_agent_disconnect_monitor"
    assert kwargs["project"] == "FOOD"
    assert kwargs["trigger_reaction"] is True
    assert "diagnostics/disconnect-stats" in kwargs["reaction_prompt"]
    assert kwargs["idempotency_key"].startswith("pc-agent-disconnect-")
    assert "125.0" not in kwargs["idempotency_key"]


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

    assert result["backend_source"] == "local+peer"
    assert result["agents"][0]["agent_id"] == "peer-pc"


@pytest.mark.asyncio
async def test_pc_agent_health_uses_peer_fallback_when_local_backend_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pc_agent, "_flush_pending_reload_disconnects", AsyncMock())
    monkeypatch.setattr(pc_agent, "_ensure_offline_monitor", Mock())
    monkeypatch.setattr(pc_agent.pc_agent_manager, "list_agents", Mock(return_value=[]))
    monkeypatch.setattr(
        pc_agent,
        "_request_peer_fallback_json",
        AsyncMock(
            return_value={
                "connected": 1,
                "agents": [
                    {
                        "agent_id": "peer-pc",
                        "hostname": "active-host",
                        "last_heartbeat": "2026-08-25T08:25:37Z",
                    }
                ],
                "backend_source": "peer",
            }
        ),
    )

    result = await pc_agent.pc_agent_health(_DummyRequest())

    assert result["connected"] == 1
    assert result["backend_source"] == "peer"
    assert result["agents"][0]["agent_id"] == "peer-pc"


@pytest.mark.asyncio
async def test_pc_agent_diagnostics_merges_peer_online_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pc_agent, "_flush_pending_reload_disconnects", AsyncMock())
    monkeypatch.setattr(
        pc_agent.pc_agent_manager,
        "list_agent_statuses",
        Mock(return_value=[{"agent_id": "local-pc", "status": "online"}]),
    )
    monkeypatch.setattr(
        pc_agent,
        "_request_peer_fallback_json",
        AsyncMock(
            return_value={
                "online_agents": [{"agent_id": "peer-pc", "status": "online"}],
                "latest_launcher_status": [{"agent_id": "peer-pc", "status": {"worker_connected": True}}],
                "latest_connection_events": [{"agent_id": "peer-pc", "event": "connected"}],
                "backend_source": "peer",
            }
        ),
    )

    result = await pc_agent.pc_agent_diagnostics(_DummyRequest())

    assert result["backend_source"] == "local+peer"
    assert result["online_count"] == 2
    assert [agent["agent_id"] for agent in result["online_agents"]] == ["local-pc", "peer-pc"]


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
    result = await pc_agent.route_execute_command(request, _internal_request())

    assert result["status"] == "success"
    assert result["command_id"] == "peer-cmd-1"
    assert result["backend_source"] == "peer"


@pytest.mark.asyncio
async def test_route_execute_uses_peer_fallback_on_local_agent_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pc_agent.pc_agent_manager,
        "execute_routed_command",
        AsyncMock(return_value={"status": "error", "error_code": "AGENT_BUSY", "message": "queue wait timeout"}),
    )
    monkeypatch.setattr(
        pc_agent,
        "_request_peer_fallback_json",
        AsyncMock(
            return_value={
                "status": "success",
                "command_id": "peer-cmd-busy",
                "result": {"status": "success", "result": {"ok": True}},
                "backend_source": "peer",
            }
        ),
    )

    request = pc_agent.RoutedCommandRequest(command_type="system_info", params={})
    result = await pc_agent.route_execute_command(request, _internal_request())

    assert result["status"] == "success"
    assert result["command_id"] == "peer-cmd-busy"
    assert result["backend_source"] == "peer"


@pytest.mark.asyncio
async def test_route_execute_browser_eval_allows_long_bank_evaluation_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_execute_routed_command(**kwargs):
        captured.update(kwargs)
        return {"status": "success", "command_id": "cmd-browser-eval"}

    monkeypatch.setattr(pc_agent.pc_agent_manager, "execute_routed_command", fake_execute_routed_command)

    request = pc_agent.RoutedCommandRequest(
        command_type="browser_eval",
        params={"expression": "document.body.innerText"},
        command_timeout_seconds=90,
    )
    result = await pc_agent.route_execute_command(request, _internal_request())

    assert result["status"] == "success"
    assert captured["command_timeout_seconds"] == 90
    assert captured["params"]["evaluate_timeout_seconds"] == 60.0


@pytest.mark.asyncio
async def test_route_execute_browser_eval_extends_financial_evaluation_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_execute_routed_command(**kwargs):
        captured.update(kwargs)
        return {"status": "success", "command_id": "cmd-bank-browser-eval"}

    monkeypatch.setattr(pc_agent.pc_agent_manager, "execute_routed_command", fake_execute_routed_command)

    request = pc_agent.RoutedCommandRequest(
        command_type="browser_eval",
        params={
            "expression": "document.body.innerText",
            "work_key": "yeoljeong-bank-shinhan-mia",
        },
        job_type="financial_exclusive",
        command_timeout_seconds=180,
    )
    result = await pc_agent.route_execute_command(request, _internal_request())

    assert result["status"] == "success"
    assert captured["command_timeout_seconds"] == 180
    assert captured["params"]["evaluate_timeout_seconds"] == 179.5


@pytest.mark.asyncio
async def test_execute_browser_command_adds_default_work_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(pc_agent.pc_agent_manager, "get_agent", lambda _agent_id: object())

    async def fake_send_command(agent_id: str, command_type: str, params: dict[str, object]) -> str:
        captured["agent_id"] = agent_id
        captured["command_type"] = command_type
        captured["params"] = params
        return "cmd-browser-1"

    monkeypatch.setattr(pc_agent.pc_agent_manager, "send_command", fake_send_command)

    request = pc_agent.CommandRequest(agent_id="ceo-pc", command_type="browser_launch", params={})
    result = await pc_agent.execute_command(request, _internal_request())

    assert result == {"command_id": "cmd-browser-1", "status": "pending"}
    assert captured["agent_id"] == "ceo-pc"
    assert captured["command_type"] == "browser_launch"
    assert captured["params"] == {"work_key": "aads-ceo-browser", "new_window": False}
