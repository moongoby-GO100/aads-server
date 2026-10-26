"""Exercise MCP preflight against real children with systemd-style stdin."""

import asyncio
import sys

import pytest

from scripts import claude_relay_server as relay


@pytest.fixture
def preflight(monkeypatch):
    monkeypatch.setattr(relay, "_MCP_PREFLIGHT_CACHE", {})
    monkeypatch.setattr(relay, "_MCP_PREFLIGHT_TIMEOUT_SEC", 0.3)
    spawn = asyncio.create_subprocess_exec
    children = []

    async def systemd_spawn(*args, **kwargs):
        # An unspecified stdin inherits the service's /dev/null, regardless
        # of the terminal or test runner that executes this regression test.
        kwargs.setdefault("stdin", asyncio.subprocess.DEVNULL)
        proc = await spawn(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(relay.asyncio, "create_subprocess_exec", systemd_spawn)
    return children


def config(code):
    return {"command": sys.executable, "args": ["-c", code], "_path_mode": "test"}


@pytest.mark.asyncio
async def test_stdio_server_survives_probe_then_exits_on_eof(preflight, tmp_path):
    marker = tmp_path / "closed"
    code = (
        "import sys,pathlib; sys.stdin.read(); "
        f"pathlib.Path({str(marker)!r}).write_text('clean shutdown')"
    )
    result = await relay._preflight_mcp_server("aads-tools", config(code))
    assert result["ok"] is True
    assert marker.read_text() == "clean shutdown"
    assert preflight[0].returncode == 0


@pytest.mark.asyncio
async def test_startup_import_failure_is_still_rejected(preflight):
    result = await relay._preflight_mcp_server(
        "aads-tools", config("import definitely_absent_mcp_dependency")
    )
    assert result["ok"] is False
    assert result["error_type"] == "python_module_missing"
    assert preflight[0].returncode != 0


@pytest.mark.asyncio
async def test_unresponsive_probe_child_is_reaped(preflight):
    result = await relay._preflight_mcp_server(
        "aads-tools", config("import time; time.sleep(30)")
    )
    assert result["ok"] is True
    assert preflight[0].returncode is not None
