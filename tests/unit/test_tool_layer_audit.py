from __future__ import annotations

import logging
from unittest.mock import patch

import pytest


class _StubRegistry:
    def __init__(self, tools: list[str]) -> None:
        self._tools = tools

    def get_all_tools(self):
        return {name: {} for name in self._tools}


@pytest.mark.asyncio
async def test_tool_layer_audit_detects_three_layer_mismatch():
    from app.services.tool_executor import ToolExecutor

    executor = ToolExecutor()
    executor._registry = _StubRegistry(["shared", "aligned_all", "registry_only"])

    with patch.object(
        executor,
        "_get_dispatch_tool_names",
        return_value={"shared", "aligned_all", "executor_only"},
    ), patch.object(
        executor,
        "_get_mcp_tool_names",
        return_value={"shared", "aligned_all", "mcp_only"},
    ):
        result = await executor._tool_layer_audit({"fix": False})

    assert result["registry_only"] == ["registry_only"]
    assert result["executor_only"] == ["executor_only"]
    assert result["mcp_only"] == ["mcp_only"]
    assert result["not_in_mcp"] == ["executor_only", "registry_only"]
    assert result["all_aligned"] is False
    assert result["total_registry"] == 3
    assert result["total_executor"] == 3
    assert result["total_mcp"] == 3
    assert result["fix"] is False
    assert result["fix_applied"] is False


@pytest.mark.asyncio
async def test_tool_layer_audit_fix_mode_logs_only(caplog: pytest.LogCaptureFixture):
    from app.services.tool_executor import ToolExecutor

    executor = ToolExecutor()
    executor._registry = _StubRegistry(["shared", "registry_only"])

    caplog.set_level(logging.INFO)

    with patch.object(
        executor,
        "_get_dispatch_tool_names",
        return_value={"shared", "executor_only"},
    ), patch.object(
        executor,
        "_get_mcp_tool_names",
        return_value={"shared"},
    ):
        result = await executor._tool_layer_audit({"fix": True})

    assert result["fix"] is True
    assert result["fix_applied"] is False
    assert "tool_layer_audit_fix_requested" in caplog.text


def test_tool_registry_exposes_audit_mapping():
    from app.services.tool_registry import ToolRegistry

    tools = ToolRegistry().get_all_tools()

    assert "query_project_database" in tools
    assert "run_remote_command" in tools
