from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

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
