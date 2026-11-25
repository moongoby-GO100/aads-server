"""MCP 브리지 레거시 폴백(execute_tool)에도 live_trading_guard 가 적용된다."""
import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


class _UnknownExecutor:
    async def execute(self, name, params):
        return json.dumps({"error": "unknown_tool"})


async def _run(name, params, guard_result=None, guard_exc=None):
    import mcp_servers.aads_tools_bridge as bridge

    legacy = AsyncMock(return_value="LEGACY_RAN")
    check = AsyncMock(return_value=guard_result, side_effect=guard_exc)
    with patch.object(bridge, "_ensure_db", new=AsyncMock()), \
         patch("app.services.tool_executor.ToolExecutor", _UnknownExecutor), \
         patch("app.services.live_trading_guard.check", new=check), \
         patch("app.api.ceo_chat_tools.execute_tool", new=legacy):
        out = await bridge._call_tool_unguarded(name, params)
    return out, legacy


@pytest.mark.asyncio
async def test_fallback_blocks_live_unit_restart():
    blocked = json.dumps({"error": "live_trading_approval_required", "blocked": True})
    out, legacy = await _run(
        "run_remote_command",
        {"project": "GO100", "command": "systemctl restart go100-card310"},
        guard_result=blocked,
    )
    assert out == blocked
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_gate_error_failcloses_for_live_restart():
    out, legacy = await _run(
        "run_remote_command",
        {"project": "GO100", "command": "systemctl restart go100-card310"},
        guard_exc=RuntimeError("db down"),
    )
    assert json.loads(out)["blocked"] is True
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_read_tool_still_runs():
    out, legacy = await _run(
        "read_remote_file", {"project": "GO100", "file_path": "/tmp/x.log"}
    )
    assert out == "LEGACY_RAN"
    legacy.assert_awaited_once()


async def _run_gate_broken(name, params):
    import mcp_servers.aads_tools_bridge as bridge

    legacy = AsyncMock(return_value="LEGACY_RAN")
    with patch.object(bridge, "_ensure_db", new=AsyncMock()), \
         patch("app.services.tool_executor.ToolExecutor", _UnknownExecutor), \
         patch.dict(sys.modules, {"app.services.live_trading_guard": None}), \
         patch("app.api.ceo_chat_tools.execute_tool", new=legacy):
        out = await bridge._call_tool_unguarded(name, params)
    return out, legacy


@pytest.mark.asyncio
async def test_gate_import_error_blocks_live_restart():
    out, legacy = await _run_gate_broken(
        "run_remote_command",
        {"project": "GO100", "command": "systemctl restart go100-card310"},
    )
    assert json.loads(out)["blocked"] is True
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_import_error_read_tool_passes():
    out, legacy = await _run_gate_broken(
        "read_remote_file", {"project": "GO100", "file_path": "/tmp/x.log"}
    )
    assert out == "LEGACY_RAN"
    legacy.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_import_error_unknown_tool_blocked():
    out, legacy = await _run_gate_broken("some_new_tool", {"x": 1})
    assert json.loads(out)["blocked"] is True
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_import_error_alias_and_canonical_both_pass():
    for n in ("query_db", "query_database"):
        out, legacy = await _run_gate_broken(n, {"query": "SELECT 1"})
        assert out == "LEGACY_RAN", n
        legacy.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_error_log_has_no_exception_text(caplog):
    import logging

    secret = RuntimeError("password=secret123 SELECT * FROM t")
    with caplog.at_level(logging.WARNING):
        out, legacy = await _run(
            "run_remote_command",
            {"project": "GO100", "command": "systemctl restart x"},
            guard_exc=secret,
        )
    assert json.loads(out)["blocked"] is True
    assert "secret123" not in caplog.text
    assert "SELECT" not in caplog.text
    assert "RuntimeError" in caplog.text
