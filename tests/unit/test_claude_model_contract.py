"""No provider calls: contract, process boundary and telemetry regression checks."""
import asyncio
import json
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.claude_model_contract import (
    AADS_MODEL_IDS, EXACT_MODEL_IDS, ModelObservation, resolve_model, runtime_alias, session_key,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("requested,expected", list(AADS_MODEL_IDS.items()) + [
    ("claude-fable-5.1", "claude-fable-5-1"),
    ("opus", "claude-opus-5"), ("sonnet", "claude-sonnet-5"),
])
def test_settings_to_exact_cli_model(requested, expected):
    assert resolve_model(requested) == expected
    assert resolve_model(runtime_alias(requested)) == expected


@pytest.mark.parametrize("model", sorted(EXACT_MODEL_IDS))
def test_explicit_version_never_changes(model):
    assert resolve_model(model) == model
    assert resolve_model(runtime_alias(model)) == model


@pytest.mark.parametrize("model", [None, "", "claude-unknown", "claude-opus-999", "gpt-5.5", "opus;echo bad"])
def test_unknown_models_fail_closed(model):
    with pytest.raises(ValueError, match="unsupported_claude_model"):
        resolve_model(model)


def test_resume_isolated_by_model_and_account_but_equivalent_aliases_share():
    assert session_key("s", "1", "claude-opus") == session_key("s", "1", "claude-opus-5")
    assert session_key("s", "1", "claude-opus") != session_key("s", "1", "claude-opus-46")
    assert session_key("s", "1", "claude-opus") != session_key("s", "2", "claude-opus")
    assert session_key("s", "0") == "s"
    assert session_key("s", "0", "claude-opus") == "s@claude-opus-5"


def test_primary_model_not_first_subagent_usage_and_mismatch_detected():
    tracker = ModelObservation("claude-opus", "claude-opus-5")
    tracker.observe({"type": "assistant", "parent_tool_use_id": "sub", "message": {"model": "claude-haiku-4-5-20251001"}})
    tracker.observe({"type": "assistant", "message": {"model": "claude-opus-4-6"}})
    evidence = tracker.observe({"type": "result", "modelUsage": {"claude-haiku-4-5-20251001": {}, "claude-opus-4-6[1m]": {}}})
    assert evidence["actual_model"] == "claude-opus-4-6"
    assert evidence["model_verified"] is True
    assert evidence["model_mismatch"] is True
    assert len(evidence["used_models"]) == 2


def test_init_and_usage_are_not_primary_response_evidence():
    tracker = ModelObservation("claude-opus", "claude-opus-5")
    assert not tracker.observe({"type": "system", "subtype": "init", "model": "claude-opus-5"})["model_verified"]
    assert tracker.observe({"type": "result", "modelUsage": {"claude-opus-5": {}}})["actual_model"] == "unverified"


def test_multiple_primary_models_are_not_arbitrarily_reduced_to_one():
    tracker = ModelObservation()
    for model in ("claude-opus-5", "claude-sonnet-5"):
        evidence = tracker.observe({"type": "assistant", "message": {"model": model}})
    assert evidence["actual_model"] == "unverified"
    assert len(evidence["used_models"]) == 2


@pytest.mark.asyncio
async def test_relay_rejects_unknown_before_launch_or_lease(monkeypatch):
    from scripts import claude_relay_server as relay
    launch = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    request = AsyncMock()
    request.json.return_value = {"model": "claude-no-such-model", "messages_text": "test"}
    response = await relay.handle_stream(request)
    assert response.status == 400
    assert json.loads(response.text)["error_type"] == "unsupported_claude_model"
    launch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", list(AADS_MODEL_IDS))
async def test_relay_passes_exact_model_to_process_and_reports_receipt(monkeypatch, requested):
    from scripts import claude_relay_server as relay
    expected = resolve_model(requested)
    proc = Mock(pid=123, returncode=0, stderr=None)
    proc.stdin.drain = AsyncMock()
    launch = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(relay, "_DIRECT_OAUTH_ENABLED", False)
    monkeypatch.setattr(relay, "_semaphore", asyncio.Semaphore(15))
    monkeypatch.setattr(relay, "_session_map", {})
    monkeypatch.setattr(relay, "_resolve_cli_command", lambda *_: {"mode": "test"})
    monkeypatch.setattr(relay, "_preflight_cli_command", lambda *_: {"ok": True})
    monkeypatch.setattr(relay, "_load_mcp_template", lambda *_: {"mcpServers": {}})
    monkeypatch.setattr(relay, "_resolve_aads_tools_cfg", AsyncMock(return_value=({"command": "fake"}, {}, [])))
    monkeypatch.setattr(relay, "_build_mcp_config", lambda *a, **kw: None)
    monkeypatch.setattr(relay, "_claude_argv_for_slot", lambda *a: ["fake-claude"])
    monkeypatch.setattr(relay, "_build_claude_env", lambda *a: {})
    monkeypatch.setattr(relay, "_stream_prepare", AsyncMock())
    writer = AsyncMock()
    monkeypatch.setattr(relay, "_stream_write", writer)
    monkeypatch.setattr(relay, "_stream_write_eof", AsyncMock())
    monkeypatch.setattr(relay, "_save_session_map", lambda: None)

    async def lines(*a, **kw):
        for event in [
            {"type": "assistant", "message": {"model": expected, "content": []}},
            {"type": "result", "session_id": "fake-cli", "modelUsage": {expected: {}}},
        ]:
            yield json.dumps(event).encode()

    monkeypatch.setattr(relay, "_iter_ndjson_lines", lines)
    request = AsyncMock()
    request.json.return_value = {"model": requested, "messages_text": "test", "session_id": "test", "model_contract_version": 1}
    response = await relay.handle_stream(request)
    assert response.status == 200
    argv = launch.call_args.args
    assert argv[argv.index("--model") + 1] == expected
    result = json.loads(writer.call_args_list[-1].args[1])
    assert result["aads_model_contract"]["actual_model"] == expected
    assert result["aads_model_contract"]["model_verified"] is True
    assert relay._session_map[session_key("test", "0", expected)] == "fake-cli"


@pytest.mark.parametrize("model", list(AADS_MODEL_IDS) + ["claude-opus-4-8", "claude-sonnet-4-5"])
def test_shell_runner_uses_same_contract_without_starting_runner(model):
    script = (ROOT / "scripts/pipeline-runner.sh").read_text()
    functions = script[script.index("normalize_runner_model() {"):script.index("is_read_only_instruction() {")]
    result = subprocess.run(
        ["bash", "-c", 'CLAUDE_MODEL_CONTRACT="$1"\n' + functions + '\nnormalize_claude_cli_model "$2"',
         "test", str(ROOT / "scripts/claude_model_contract.py"), model],
        check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip() == resolve_model(model)


@pytest.mark.parametrize("model", list(AADS_MODEL_IDS))
def test_python_runner_and_registry_agree(model):
    from app.services import model_registry, pipeline_runner_service
    expected = resolve_model(model)
    assert pipeline_runner_service._normalize_claude_cli_model(model) == expected
    assert model_registry._ANTHROPIC_RUNTIME_MODEL_IDS[model] == expected


@pytest.mark.asyncio
async def test_stale_registry_alias_cannot_change_explicit_version(monkeypatch):
    from app.services import model_selector
    monkeypatch.setattr(model_selector, "_list_registered_models", AsyncMock(return_value=[{
        "provider": "anthropic", "model_id": "claude-opus", "execution_model_id": "claude-opus-5",
        "metadata": {"accepted_aliases": ["claude-opus-4-8", "claude-opus-5"]},
    }]))
    model, row = await model_selector._resolve_registered_model_alias("claude-opus-4-8")
    assert model == "claude-opus-4-8"
    assert row is None


@pytest.mark.asyncio
async def test_api_rejects_old_relay_before_model_request(monkeypatch):
    from app.services import model_selector
    client = AsyncMock()
    client.get.return_value = Mock(status_code=200, json=lambda: {"status": "ok"})
    client.stream = Mock()
    factory = Mock()
    factory.return_value.__aenter__ = AsyncMock(return_value=client)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(model_selector.httpx, "AsyncClient", factory)
    events = [event async for event in model_selector._stream_cli_relay_once(
        "claude-opus", "", [{"role": "user", "content": "test"}],
    )]
    assert "model_contract_version_mismatch" in events[-1]["content"]
    client.stream.assert_not_called()
