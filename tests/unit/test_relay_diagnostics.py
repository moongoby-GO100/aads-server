from __future__ import annotations

import importlib.util
from pathlib import Path

from app.services.model_selector import _classify_relay_tool_result
from app.services.pipeline_runner_client import (
    get_pipeline_runner_api_url,
    get_pipeline_runner_base_url,
)


def _load_claude_relay_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "claude_relay_server.py"
    spec = importlib.util.spec_from_file_location("claude_relay_server_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pipeline_runner_internal_url_defaults_to_8080(monkeypatch) -> None:
    monkeypatch.delenv("PIPELINE_RUNNER_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("AADS_API_INTERNAL_URL", raising=False)
    assert get_pipeline_runner_base_url() == "http://localhost:8080"
    assert get_pipeline_runner_api_url("jobs") == "http://localhost:8080/api/v1/pipeline/jobs"


def test_pipeline_runner_internal_url_respects_override(monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_RUNNER_INTERNAL_BASE_URL", "http://runner.internal:19090/")
    assert get_pipeline_runner_base_url() == "http://runner.internal:19090"
    assert get_pipeline_runner_api_url("/jobs/batch") == "http://runner.internal:19090/api/v1/pipeline/jobs/batch"


def test_python_direct_mcp_cfg_keeps_python_args() -> None:
    relay = _load_claude_relay_module()
    cfg = {
        "command": "python3.11",
        "args": ["-m", "mcp_servers.aads_tools_bridge"],
        "env": {},
    }
    updated = relay._inject_session_into_cfg(cfg, "session-1234")
    assert updated["args"] == ["-m", "mcp_servers.aads_tools_bridge"]
    assert updated["env"]["AADS_SESSION_ID"] == "session-1234"


def test_relay_tool_result_cancel_is_reclassified_to_session_scope() -> None:
    classified = _classify_relay_tool_result(
        "user cancelled MCP tool call",
        session_id="12345678-1234-1234-1234-123456789abc",
        relay_name="claude",
        tool_name="read_remote_file",
    )
    assert classified["is_error"] is True
    assert classified["error_type"] == "session_cancelled_mcp_tool_call"
    assert classified["cancel_scope"] == "session"
    assert "relay=claude" in classified["content"]
