from __future__ import annotations

import json

import pytest

from app.api import ceo_chat_tools
from app.services import tool_registry
from app.services.tool_executor import ToolExecutor


def test_tool_registry_descriptions_expose_windows_command_aliases() -> None:
    pc_tool = tool_registry._TOOLS["pc_execute"]
    device_tool = tool_registry._TOOLS["device_execute"]

    assert "cmd" in pc_tool["description"]
    assert "powershell" in pc_tool["description"]
    assert "app_launch" in pc_tool["description"]
    assert "cmd" in device_tool["description"]
    assert "powershell" in device_tool["description"]


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
