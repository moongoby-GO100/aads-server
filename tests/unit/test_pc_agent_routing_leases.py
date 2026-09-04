from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.models.pc_agent import CommandResult
from app.services.pc_agent_manager import PCAgentManager


@pytest.fixture(autouse=True)
def _clear_default_pc_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PC_AGENT_DEFAULT_AGENT_ID", raising=False)
    monkeypatch.delenv("PC_AGENT_DEFAULT_HOSTNAME", raising=False)


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
async def test_execute_routed_command_does_not_auto_select_other_owner_agent() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "other-user-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "other", "capabilities": ["chrome_cdp", "interactive_browser"]},
        owner_user_id="user-b",
    )

    result = await manager.execute_routed_command(
        command_type="browser_eval",
        params={"expression": "document.title"},
        job_type="managed_browser",
        required_capabilities=["interactive_browser"],
        owner_user_id="user-a",
        command_timeout_seconds=0.1,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "PC_AGENT_OFFLINE"


@pytest.mark.asyncio
async def test_execute_routed_command_rejects_preferred_other_owner_agent() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "other-user-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "other", "capabilities": ["chrome_cdp", "interactive_browser"]},
        owner_user_id="user-b",
    )

    result = await manager.execute_routed_command(
        command_type="browser_eval",
        params={"expression": "document.title"},
        agent_id="other-user-pc",
        job_type="managed_browser",
        required_capabilities=["interactive_browser"],
        owner_user_id="user-a",
        command_timeout_seconds=0.1,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "AGENT_FORBIDDEN"


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


def test_list_agent_statuses_exposes_route_ready_agent_shape() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "oby-ceo",
        ws,  # type: ignore[arg-type]
        {
            "hostname": "ceo",
            "capabilities": ["chrome_cdp", "interactive_browser"],
            "command_types": ["browser_launch", "browser_tabs", "browser_close_session"],
        },
    )

    statuses = manager.list_agent_statuses()

    assert statuses[0]["agent_id"] == "oby-ceo"
    assert statuses[0]["status"] == "online"
    assert {"chrome_cdp", "interactive_browser"} <= set(statuses[0]["capabilities"])
    assert {"browser_launch", "browser_tabs", "browser_close_session"} <= set(statuses[0]["command_types"])


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
    assert payload["params"]["command"].startswith("powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand ")


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
async def test_browser_bridge_work_keys_serialize_per_agent() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "icu55hk",
        ws,  # type: ignore[arg-type]
        {"hostname": "DESKTOP-ICU55HK", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    bank = await manager.acquire_lease(
        preferred_agent_id="icu55hk",
        job_type="browser_bridge_bank_session",
        command_type="browser_eval",
        required_capabilities=["interactive_browser"],
    )
    chat = await manager.acquire_lease(
        preferred_agent_id="icu55hk",
        job_type="browser_bridge_chat_plan_check",
        command_type="browser_eval",
        required_capabilities=["interactive_browser"],
    )

    assert bank["status"] == "running"
    assert bank["lease"]["job_type"] == "financial_exclusive"
    assert chat["status"] == "queued"
    assert chat["lease"]["job_type"] == "browser_bridge"


@pytest.mark.asyncio
async def test_financial_exclusive_blocks_managed_browser_on_same_agent() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "icu55hk",
        ws,  # type: ignore[arg-type]
        {"hostname": "DESKTOP-ICU55HK", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    financial = await manager.acquire_lease(
        preferred_agent_id="icu55hk",
        job_type="authenticated_collector_banking_shinhan",
        command_type="browser_eval",
        required_capabilities=["interactive_browser"],
    )
    managed = await manager.acquire_lease(
        preferred_agent_id="icu55hk",
        job_type="managed_browser",
        command_type="browser_eval",
        required_capabilities=["interactive_browser"],
    )

    assert financial["status"] == "running"
    assert financial["lease"]["job_type"] == "financial_exclusive"
    assert managed["status"] == "queued"
    assert managed["lease"]["queue_position"] == 1


@pytest.mark.asyncio
async def test_bank_work_key_promotes_browser_bridge_route_to_financial_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "icu55hk",
        ws,  # type: ignore[arg-type]
        {"hostname": "DESKTOP-ICU55HK", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    async def fake_send_command(_agent_id: str, _command_type: str, _params: dict[str, object]) -> str:
        return "cmd-1"

    async def fake_get_result(command_id: str, timeout: float = 30.0) -> CommandResult:
        return CommandResult(
            command_id=command_id,
            agent_id="icu55hk",
            status="success",
            result={"ok": True},
        )

    monkeypatch.setattr(manager, "send_command", fake_send_command)
    monkeypatch.setattr(manager, "get_result", fake_get_result)

    result = await manager.execute_routed_command(
        command_type="browser_launch",
        params={
            "work_key": "yeoljeong-bank-shinhan-mia",
            "url": "https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
        },
        agent_id="icu55hk",
        job_type="browser_bridge_launch",
        required_capabilities=["interactive_browser"],
        command_timeout_seconds=5.0,
    )

    assert result["status"] == "success"
    assert result["lease"]["job_type"] == "financial_exclusive"


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
async def test_late_pc_agent_result_does_not_revive_timed_out_command() -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "ceo", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    command_id = await manager.send_command(
        "ceo-pc",
        "browser_eval",
        {"expression": "document.title"},
    )

    timed_out = await manager.get_result(command_id, timeout=0.01)
    assert timed_out.status == "timeout"

    manager.receive_result(command_id, {"status": "success", "data": {"title": "late"}})
    stored = await manager.get_result(command_id, timeout=0.01)

    assert stored.status == "timeout"
    assert stored.result == {
        "late_result": True,
        "late_status": "success",
        "late_data": {"title": "late"},
    }


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
async def test_execute_routed_command_timeout_triggers_browser_close_session(
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
    assert sent[1][0] == "browser_close_session"
    assert sent[1][1]["work_key"] == "ntv2-vvic-scrape"
    assert sent[1][1]["close_browser"] is False
    assert sent[1][1]["close_tabs"] is False


@pytest.mark.asyncio
async def test_execute_routed_command_timeout_falls_back_to_browser_health_cleanup(
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
        if command_id == "cmd-2":
            return CommandResult(
                command_id=command_id,
                agent_id="ceo-pc",
                status="error",
                result={"error": "close failed"},
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
    assert [item[0] for item in sent] == [
        "browser_eval",
        "browser_close_session",
        "browser_health",
    ]
    assert sent[2][1]["work_key"] == "ntv2-vvic-scrape"
    assert sent[2][1]["cleanup"] is True


@pytest.mark.asyncio
async def test_execute_routed_command_timeout_falls_back_when_close_does_not_release_session(
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
        if command_id == "cmd-2":
            return CommandResult(
                command_id=command_id,
                agent_id="ceo-pc",
                status="success",
                result={"session_released": False, "guard_released": None},
            )
        return CommandResult(
            command_id=command_id,
            agent_id="ceo-pc",
            status="success",
            result={"session_released": True, "guard_released": True},
        )

    monkeypatch.setattr(manager, "send_command", fake_send_command)
    monkeypatch.setattr(manager, "get_result", fake_get_result)

    result = await manager.execute_routed_command(
        command_type="browser_eval",
        params={
            "expression": "document.title",
            "work_key": "yeoljeong-bank-shinhan-individual-test",
        },
        command_timeout_seconds=5.0,
        lease_ttl_seconds=35,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "COMMAND_TIMEOUT"
    assert [item[0] for item in sent] == [
        "browser_eval",
        "browser_close_session",
        "browser_health",
    ]
    assert sent[2][1]["cleanup"] is True


@pytest.mark.asyncio
async def test_execute_routed_command_browser_launch_timeout_closes_browser_session(
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
            result={"session_released": True, "guard_released": True},
        )

    monkeypatch.setattr(manager, "send_command", fake_send_command)
    monkeypatch.setattr(manager, "get_result", fake_get_result)

    result = await manager.execute_routed_command(
        command_type="browser_launch",
        params={
            "url": "https://bank.shinhan.com/rib/easy/index.jsp#210000000000",
            "work_key": "yeoljeong-bank-shinhan-individual-test",
        },
        command_timeout_seconds=5.0,
        lease_ttl_seconds=35,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "COMMAND_TIMEOUT"
    assert len(sent) == 2
    assert sent[1][0] == "browser_close_session"
    assert sent[1][1]["close_browser"] is True
    assert sent[1][1]["close_tabs"] is True


@pytest.mark.asyncio
async def test_vvic_browser_launch_reuses_work_key_profile_without_new_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = PCAgentManager()
    ws = _DummyWebSocket()
    manager.register_agent(
        "ceo-pc",
        ws,  # type: ignore[arg-type]
        {"hostname": "ceo", "capabilities": ["vvic", "chrome_cdp", "interactive_browser"]},
    )

    observed: dict[str, object] = {}

    async def fake_send_command(_agent_id: str, command_type: str, params: dict[str, object]) -> str:
        observed["command_type"] = command_type
        observed["params"] = dict(params)
        return "cmd-1"

    async def fake_get_result(command_id: str, timeout: float = 30.0) -> CommandResult:
        return CommandResult(
            command_id=command_id,
            agent_id="ceo-pc",
            status="success",
            result={"port": 9222, "ok": True},
        )

    monkeypatch.setattr(manager, "send_command", fake_send_command)
    monkeypatch.setattr(manager, "get_result", fake_get_result)

    result = await manager.execute_routed_command(
        command_type="browser_launch",
        params={"work_key": "ntv2-vvic-scrape"},
        job_type="vvic_cdp",
        required_capabilities=["vvic", "chrome_cdp", "interactive_browser"],
        command_timeout_seconds=5.0,
        lease_ttl_seconds=35,
    )

    assert result["status"] == "success"
    assert observed["command_type"] == "browser_launch"
    sent_params = observed["params"]
    assert isinstance(sent_params, dict)
    assert sent_params["work_key"] == "ntv2-vvic-scrape"
    assert sent_params["isolation_id"] == "ntv2-vvic-scrape"
    assert sent_params["new_window"] is False


@pytest.mark.asyncio
async def test_execute_routed_command_close_on_complete_triggers_session_cleanup(
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
            "work_key": "aads-ceo-browser",
            "close_on_complete": True,
        },
        command_timeout_seconds=5.0,
        lease_ttl_seconds=35,
    )

    assert result["status"] == "success"
    assert len(sent) == 2
    assert sent[0][0] == "browser_eval"
    assert sent[1][0] == "browser_close_session"
    assert sent[1][1]["work_key"] == "aads-ceo-browser"
    assert sent[1][1]["close_browser"] is False


@pytest.mark.asyncio
async def test_execute_routed_command_error_close_on_complete_triggers_session_cleanup(
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
                status="error",
                result={"error": "page crashed", "error_code": "BROWSER_COMMAND_FAILED"},
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
            "work_key": "aads-ceo-browser",
            "close_on_complete": True,
        },
        command_timeout_seconds=5.0,
        lease_ttl_seconds=35,
    )

    assert result["status"] == "error"
    assert len(sent) == 2
    assert sent[0][0] == "browser_eval"
    assert sent[1][0] == "browser_close_session"
    assert sent[1][1]["work_key"] == "aads-ceo-browser"


@pytest.mark.asyncio
async def test_browser_jobs_prefer_configured_default_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PC_AGENT_DEFAULT_AGENT_ID", "ceo-pc")
    monkeypatch.setenv("PC_AGENT_DEFAULT_HOSTNAME", "DESKTOP-NPC6JAT")
    manager = PCAgentManager()
    ceo_ws = _DummyWebSocket()
    other_ws = _DummyWebSocket()
    manager.register_agent(
        "other-pc",
        other_ws,  # type: ignore[arg-type]
        {"hostname": "DESKTOP-TBKF5M3", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )
    manager.register_agent(
        "ceo-pc",
        ceo_ws,  # type: ignore[arg-type]
        {"hostname": "DESKTOP-NPC6JAT", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    observed: dict[str, object] = {}

    async def fake_send_command(agent_id: str, command_type: str, params: dict[str, object]) -> str:
        observed["agent_id"] = agent_id
        observed["command_type"] = command_type
        observed["params"] = dict(params)
        return "cmd-1"

    async def fake_get_result(command_id: str, timeout: float = 30.0) -> CommandResult:
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
        params={"expression": "document.title"},
        job_type="managed_browser",
        required_capabilities=["interactive_browser"],
        command_timeout_seconds=5.0,
        lease_ttl_seconds=35,
    )

    assert result["status"] == "success"
    assert observed["agent_id"] == "ceo-pc"


@pytest.mark.asyncio
async def test_browser_jobs_do_not_fallback_when_configured_default_agent_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PC_AGENT_DEFAULT_AGENT_ID", "ceo-pc")
    monkeypatch.setenv("PC_AGENT_DEFAULT_HOSTNAME", "DESKTOP-TBKF5M3")
    manager = PCAgentManager()
    other_ws = _DummyWebSocket()
    manager.register_agent(
        "other-pc",
        other_ws,  # type: ignore[arg-type]
        {"hostname": "DESKTOP-TBKF5M3", "capabilities": ["chrome_cdp", "interactive_browser"]},
    )

    result = await manager.acquire_lease(
        job_type="managed_browser",
        command_type="browser_eval",
        required_capabilities=["interactive_browser"],
        ttl_seconds=35,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "PC_AGENT_OFFLINE"
    assert result["message"] == "default browser PC agent 'ceo-pc' is offline"
