from __future__ import annotations

import json
import inspect

import pytest

from app.api import ceo_chat_tools
from app.browser_bridge.service import _LocalAgentPage
from app.services import tool_registry
from app.services.tool_executor import ToolExecutor


def test_tool_registry_descriptions_expose_windows_command_aliases() -> None:
    pc_tool = tool_registry._TOOLS["pc_execute"]
    device_tool = tool_registry._TOOLS["device_execute"]

    assert "cmd" in pc_tool["description"]
    assert "powershell" in pc_tool["description"]
    assert "app_launch" in pc_tool["description"]
    assert "file_upload" in pc_tool["description"]
    assert "file_download" in pc_tool["description"]
    assert "cmd" in device_tool["description"]
    assert "powershell" in device_tool["description"]
    assert "file_upload" in device_tool["description"]
    assert "file_download" in device_tool["description"]


def test_ceo_chat_tools_exposes_pc_and_device_execute() -> None:
    names = {tool["name"] for tool in ceo_chat_tools.TOOL_DEFINITIONS}
    assert {"pc_execute", "device_execute"} <= names


@pytest.mark.asyncio
async def test_ceo_chat_tools_pc_execute_delegates_to_tool_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_dispatch(self, name: str, params: dict[str, object]):  # noqa: ANN001
        assert name == "pc_execute"
        assert params["command_type"] == "system_info"
        return {"status": "success", "command_id": "pc-1"}

    monkeypatch.setattr(ToolExecutor, "_dispatch", fake_dispatch)

    result = await ceo_chat_tools.execute_tool("pc_execute", {"command_type": "system_info"}, dsn="")

    assert json.loads(result) == {"status": "success", "command_id": "pc-1"}


@pytest.mark.asyncio
async def test_device_execute_routes_pc_command_to_pc_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = ToolExecutor()

    async def fake_execute_routed_command(**kwargs):  # noqa: ANN003
        assert kwargs["command_type"] == "powershell"
        assert kwargs["params"] == {"command": "Get-Process"}
        return {"status": "success", "backend": "pc_agent", "command_id": "pc-2"}

    async def fail_send_command(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("device_manager.send_command should not be used for PC commands without a device target")

    monkeypatch.setattr("app.services.pc_agent_manager.pc_agent_manager.execute_routed_command", fake_execute_routed_command)
    monkeypatch.setattr("app.services.pc_agent_manager.pc_agent_manager.online_agents_count", lambda: 1)
    monkeypatch.setattr("app.services.device_manager.device_manager.get_device", lambda _agent_id: None)
    monkeypatch.setattr("app.services.device_manager.device_manager.send_command", fail_send_command)

    result = await executor._device_execute(
        {
            "command_type": "powershell",
            "params": {"command": "Get-Process"},
        }
    )

    assert result["status"] == "success"
    assert result["backend"] == "pc_agent"


@pytest.mark.asyncio
@pytest.mark.parametrize("command_type", ["file_upload", "file_download"])
async def test_device_execute_routes_file_transfer_to_pc_agent(
    monkeypatch: pytest.MonkeyPatch,
    command_type: str,
) -> None:
    executor = ToolExecutor()

    async def fake_execute_routed_command(**kwargs):  # noqa: ANN003
        assert kwargs["command_type"] == command_type
        return {"status": "success", "backend": "pc_agent"}

    async def fail_send_command(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("file transfer must be routed to PC Agent")

    monkeypatch.setattr("app.services.pc_agent_manager.pc_agent_manager.execute_routed_command", fake_execute_routed_command)
    monkeypatch.setattr("app.services.pc_agent_manager.pc_agent_manager.online_agents_count", lambda: 1)
    monkeypatch.setattr("app.services.device_manager.device_manager.get_device", lambda _agent_id: None)
    monkeypatch.setattr("app.services.device_manager.device_manager.send_command", fail_send_command)

    result = await executor._device_execute({"command_type": command_type, "params": {}})

    assert result == {"status": "success", "backend": "pc_agent"}


@pytest.mark.asyncio
async def test_device_list_includes_pc_agent_manager_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = ToolExecutor()
    monkeypatch.setattr("app.services.device_manager.device_manager.get_devices", lambda _device_type: [])
    monkeypatch.setattr(
        "app.services.pc_agent_manager.pc_agent_manager.list_agent_statuses",
        lambda: [
            {
                "agent_id": "ceo-pc",
                "hostname": "ceo-desktop",
                "os_info": "Windows 11",
                "capabilities": ["interactive_browser"],
                "status": "online",
                "connected_at": "2026-07-23T00:00:00Z",
                "last_seen": "2026-07-23T00:00:05Z",
                "heartbeat_age_seconds": 1.2,
            }
        ],
    )

    result = await executor._device_list({"device_type": "pc"})

    assert result["count"] == 1
    assert result["devices"][0]["agent_id"] == "ceo-pc"
    assert result["devices"][0]["device_type"] == "pc"
    assert result["devices"][0]["status"] == "online"


@pytest.mark.asyncio
async def test_device_list_uses_api_fallback_outside_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = ToolExecutor()
    monkeypatch.setattr("app.services.device_manager.device_manager.get_devices", lambda _device_type: [])
    monkeypatch.setattr(
        "app.services.pc_agent_manager.pc_agent_manager.list_agent_statuses",
        lambda: [],
    )

    async def fake_fetch_statuses() -> list[dict[str, object]]:
        return [
            {
                "agent_id": "ceo-pc",
                "hostname": "ceo-desktop",
                "os_info": "Windows 11",
                "capabilities": ["interactive_browser"],
                "status": "online",
                "connected_at": "2026-07-23T00:00:00Z",
                "last_seen": "2026-07-23T00:00:05Z",
                "heartbeat_age_seconds": 1.2,
            }
        ]

    monkeypatch.setattr(
        "app.services.tool_executor._fetch_pc_agent_statuses_from_api",
        fake_fetch_statuses,
    )

    result = await executor._device_list({"device_type": "pc"})

    assert result["count"] == 1
    assert result["devices"][0]["agent_id"] == "ceo-pc"
    assert result["devices"][0]["status"] == "online"


@pytest.mark.asyncio
async def test_browser_connect_forwards_tenant_id(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, object] = {}

    async def fake_browser_connect(**kwargs):  # noqa: ANN003
        received.update(kwargs)
        return "ok"

    monkeypatch.setattr(ceo_chat_tools, "tool_browser_connect", fake_browser_connect)

    result = await ToolExecutor()._browser_connect(
        {
            "action": "ensure_pc_cdp",
            "agent_id": "ceo-pc",
            "url": "https://aads.newtalk.kr/chat/session-id",
            "tenant_id": "tenant-1",
        }
    )

    assert result == "ok"
    assert received["tenant_id"] == "tenant-1"


def test_browser_connect_preserves_chat_fragment_in_source() -> None:
    source = inspect.getsource(ceo_chat_tools.tool_browser_connect)

    assert '_redirect += "#" + _parsed.fragment' in source


class _FakeAadsAuthPage:
    def __init__(self) -> None:
        self.goto_calls: list[tuple[str, str]] = []
        self.evaluate_token = ""

    async def goto(self, url: str, *, wait_until: str) -> None:
        self.goto_calls.append((url, wait_until))

    async def evaluate(self, _script: str, token: str) -> None:
        self.evaluate_token = token


@pytest.mark.asyncio
async def test_apply_aads_e2e_url_injects_token_and_preserves_fragment() -> None:
    page = _FakeAadsAuthPage()

    target = await ceo_chat_tools._apply_aads_e2e_url(
        page,
        "https://aads.newtalk.kr/e2e-auth.html?token=secret-token&redirect=/chat%23session-1",
    )

    assert page.evaluate_token == "secret-token"
    assert page.goto_calls == [
        ("https://aads.newtalk.kr/e2e-auth.html", "domcontentloaded"),
        ("https://aads.newtalk.kr/chat#session-1", "domcontentloaded"),
    ]
    assert target == "https://aads.newtalk.kr/chat#session-1"


@pytest.mark.asyncio
async def test_local_agent_evaluate_accepts_playwright_argument(monkeypatch) -> None:
    page = object.__new__(_LocalAgentPage)
    captured: dict[str, str] = {}

    async def fake_run(command_type, params, **_kwargs):
        captured["command_type"] = command_type
        captured["expression"] = params["expression"]
        return {"value": "ok"}

    monkeypatch.setattr(page, "_run_browser_command", fake_run)

    result = await page.evaluate("(token) => token", "secret-token")

    assert result == "ok"
    assert captured["command_type"] == "browser_eval"
    assert "secret-token" not in captured["expression"][:200]
    assert captured["expression"].endswith('(secret-token)') is False
    assert captured["expression"].endswith('(\"secret-token\")')
