from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from app.services.model_selector import _classify_relay_tool_result
from app.services.model_selector import _normalize_codex_project as _normalize_model_selector_codex_project
from app.services.pipeline_runner_client import (
    get_pipeline_runner_api_url,
    get_pipeline_runner_base_url,
)


def _load_claude_relay_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "claude_relay_server.py"
    if "aiohttp" not in sys.modules and importlib.util.find_spec("aiohttp") is None:
        aiohttp_stub = types.ModuleType("aiohttp")
        aiohttp_stub.web = types.SimpleNamespace()
        sys.modules["aiohttp"] = aiohttp_stub
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


def test_codex_home_uses_ops_sandbox_defaults(tmp_path, monkeypatch) -> None:
    relay = _load_claude_relay_module()
    monkeypatch.setattr(relay, "_CODEX_HOME_ROOT", tmp_path)
    monkeypatch.setattr(relay, "_CODEX_SANDBOX_MODE", "danger-full-access")
    monkeypatch.setattr(relay, "_CODEX_APPROVAL_POLICY", "never")

    home = relay._build_codex_home(
        "session-1234",
        mcp_cfg={
            "mcpServers": {
                "aads-tools": {
                    "command": "python3.11",
                    "args": ["-m", "mcp_servers.aads_tools_bridge"],
                    "env": {"AADS_SESSION_ID": "session-1234"},
                }
            }
        },
    )

    config_text = (Path(home) / ".codex" / "config.toml").read_text()
    assert 'approval_policy = "never"' in config_text
    assert 'sandbox_mode = "danger-full-access"' in config_text


def test_model_selector_resolves_codex_project_from_workspace_settings() -> None:
    assert _normalize_model_selector_codex_project(
        "[GO100] 백억이",
        '{"project_key": "GO100", "workdir": "/root/kis-autotrade-v4"}',
    ) == "GO100"
    assert _normalize_model_selector_codex_project("[NAS] Image", {}) == "NAS"
    assert _normalize_model_selector_codex_project("unknown workspace", {}) == "AADS"


def test_codex_relay_cwd_falls_back_to_default(tmp_path, monkeypatch) -> None:
    relay = _load_claude_relay_module()
    monkeypatch.setattr(relay, "_CODEX_DEFAULT_CWD", str(tmp_path))
    monkeypatch.setattr(relay, "_CODEX_CWD_MAP", {"KIS": "/missing/kis"})

    assert relay._resolve_codex_cwd("KIS") == str(tmp_path)


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
