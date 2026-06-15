from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.models.pc_agent import CommandResult
from app.services.pc_agent_manager import PCAgentManager


class _DummyWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.messages.append(payload)


@pytest.mark.asyncio
async def test_execute_routed_command_returns_offline_when_no_agent() -> None:
    manager = PCAgentManager()

    result = await manager.execute_routed_command(
        command_type="browser_launch",
        job_type="vvic_cdp",
        required_capabilities=["vvic", "chrome_cdp"],
        command_timeout_seconds=0.1,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "PC_AGENT_OFFLINE"


@pytest.mark.asyncio
async def test_execute_routed_command_returns_no_capable_agent() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "ceo", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    result = await manager.execute_routed_command(
        command_type="browser_launch",
        job_type="vvic_cdp",
        required_capabilities=["vvic"],
    )

    assert result["status"] == "error"
    assert result["error_code"] == "NO_CAPABLE_AGENT"


def test_register_agent_status_exposes_shell_alias_command_types() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {
            "hostname": "ceo",
            "capabilities": ["pc_control"],
            "command_types": ["shell", "system_info", "app_launch"],
        },
    )

    status = manager.get_agent_status("ceo-pc")

    assert status is not None
    assert status["status"] == "online"
    assert {"shell", "cmd", "powershell", "system_info", "app_launch"} <= set(status["command_types"])
    assert status["heartbeat_age_seconds"] >= 0
    assert status["last_seen"]


@pytest.mark.asyncio
async def test_send_command_normalizes_powershell_alias() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "ceo", "capabilities": ["pc_control"], "command_types": ["shell"]},
    )

    await manager.send_command("ceo-pc", "powershell", {"command": "Get-Process"})

    assert ws.messages
    payload = ws.messages[0]["payload"]
    assert payload["command_type"] == "shell"
    assert payload["params"]["command"].startswith("powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ")


@pytest.mark.asyncio
async def test_vvic_queue_serializes_per_agent_and_promotes_next() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "ceo", "capabilities": ["vvic", "chrome_cdp", "interactive_browser"]},
    )

    lease1 = await manager.acquire_lease(
        job_type="vvic_cdp",
        command_type="browser_launch",
        required_capabilities=["vvic"],
    )
    lease2 = await manager.acquire_lease(
        job_type="vvic_cdp",
        command_type="browser_launch",
        required_capabilities=["vvic"],
    )

    assert lease1["status"] == "running"
    assert lease2["status"] == "queued"

    lease1_id = lease1["lease"]["lease_id"]
    lease2_id = lease2["lease"]["lease_id"]

    await manager.release_lease(lease1_id, status="completed")
    promoted = await manager.wait_for_lease_turn(lease2_id, timeout_seconds=0.5)

    assert promoted["status"] == "running"
    assert promoted["lease"]["lease_id"] == lease2_id


@pytest.mark.asyncio
async def test_stale_running_lease_is_reclaimed_before_new_request() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "ceo", "capabilities": ["vvic", "chrome_cdp", "interactive_browser"]},
    )

    first = await manager.acquire_lease(
        job_type="vvic_cdp",
        command_type="browser_launch",
        required_capabilities=["vvic"],
        ttl_seconds=30,
    )
    lease1_id = first["lease"]["lease_id"]

    async with manager._lease_lock:  # type: ignore[attr-defined]
        manager._leases[lease1_id].expires_at = manager._now() - timedelta(seconds=1)  # type: ignore[attr-defined]

    second = await manager.acquire_lease(
        job_type="vvic_cdp",
        command_type="browser_launch",
        required_capabilities=["vvic"],
    )

    assert second["status"] == "running"
    first_lease = await manager.get_lease(lease1_id)
    assert first_lease is not None
    assert first_lease["status"] == "expired"
    assert first_lease["error_code"] == "LEASE_EXPIRED"


@pytest.mark.asyncio
async def test_execute_routed_command_maps_timeout_to_command_timeout() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "ceo", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    result = await manager.execute_routed_command(
        command_type="browser_tabs",
        job_type="general",
        command_timeout_seconds=0.1,
        lease_ttl_seconds=30,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "COMMAND_TIMEOUT"


@pytest.mark.asyncio
async def test_pending_command_is_failed_immediately_when_agent_disconnects() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "ceo", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    command_id = await manager.send_command("ceo-pc", "browser_eval", {"expression": "document.readyState"})
    wait_task = asyncio.create_task(manager.get_result(command_id, timeout=5.0))

    assert manager.unregister_agent("ceo-pc", ws) is True

    result = await wait_task
    assert result.status == "error"
    assert result.result is not None
    assert result.result["error_code"] == "PC_AGENT_OFFLINE"


@pytest.mark.asyncio
async def test_execute_routed_command_enforces_route_timeout_upper_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "ceo", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    observed: dict[str, object] = {}

    async def fake_send_command(agent_id: str, command_type: str, params: dict[str, object]) -> str:
        observed["agent_id"] = agent_id
        observed["command_type"] = command_type
        observed["params"] = dict(params)
        return "cmd-1"

    async def fake_get_result(command_id: str, timeout: float = 30.0) -> CommandResult:
        observed["wait_timeout"] = timeout
        return CommandResult(
            command_id=command_id,
            agent_id="ceo-pc",
            status="success",
            result={"ok": True},
        )

    monkeypatch.setattr(manager, "send_command", fake_send_command)
    monkeypatch.setattr(manager, "get_result", fake_get_result)

    result = await manager.execute_routed_command(
        command_type="browser_eval",
        params={
            "expression": "document.title",
            "work_key": "ntv2-vvic-scrape",
            "command_timeout_seconds": 90,
        },
        command_timeout_seconds=5.0,
        lease_ttl_seconds=35,
    )

    assert result["status"] == "success"
    sent_params = observed["params"]
    assert isinstance(sent_params, dict)
    assert sent_params["command_timeout_seconds"] == 5.0
    assert observed["wait_timeout"] == 5.0


@pytest.mark.asyncio
async def test_execute_routed_command_timeout_triggers_browser_health_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "ceo", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    sent: list[tuple[str, dict[str, object]]] = []

    async def fake_send_command(_agent_id: str, command_type: str, params: dict[str, object]) -> str:
        sent.append((command_type, dict(params)))
        return f"cmd-{len(sent)}"

    async def fake_get_result(command_id: str, timeout: float = 30.0) -> CommandResult:
        if command_id == "cmd-1":
            return CommandResult(
                command_id=command_id,
                agent_id="ceo-pc",
                status="timeout",
                result=None,
            )
        return CommandResult(
            command_id=command_id,
            agent_id="ceo-pc",
            status="success",
            result={"ok": True},
        )

    monkeypatch.setattr(manager, "send_command", fake_send_command)
    monkeypatch.setattr(manager, "get_result", fake_get_result)

    result = await manager.execute_routed_command(
        command_type="browser_eval",
        params={
            "expression": "document.title",
            "work_key": "ntv2-vvic-scrape",
        },
        command_timeout_seconds=5.0,
        lease_ttl_seconds=35,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "COMMAND_TIMEOUT"
    assert len(sent) == 2
    assert sent[0][0] == "browser_eval"
    assert sent[1][0] == "browser_health"
    assert sent[1][1]["work_key"] == "ntv2-vvic-scrape"
    assert sent[1][1]["cleanup"] is True
