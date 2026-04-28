from __future__ import annotations

import json

import pytest

from app.services import model_selector
from app.services.intent_router import IntentResult


async def _collect_claude_route(monkeypatch, *, intent: str, model: str, use_tools: bool, tool_group: str):
    routed_models = []

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"claude-haiku", "claude-sonnet", "claude-opus"}

    async def _fake_registry_row(_model_id: str, provider=None):
        return None

    async def _fake_claude_slots():
        return {}

    async def _fake_cli_stream(target_model, system_prompt, messages, tools=None, session_id=None, oauth_slot=None):
        routed_models.append(target_model)
        assert system_prompt
        assert messages[-1]["role"] == "user"
        yield {"type": "done", "model": target_model, "cost": "0", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _fake_registry_row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", _fake_claude_slots)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", _fake_cli_stream)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent=intent, model=model, use_tools=use_tools, tool_group=tool_group),
            "system prompt",
            [{"role": "user", "content": "라우팅 확인"}],
        )
    ]

    return routed_models, events


@pytest.mark.asyncio
async def test_call_stream_routes_dynamic_qwen_model_to_direct_provider(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"qwen3.6-plus"}

    async def _fake_registry_row(model_id: str, provider=None):
        assert model_id == "qwen3.6-plus"
        return {
            "provider": "qwen",
            "model_id": model_id,
            "metadata": {
                "execution_backend": "openai_compatible_direct",
                "execution_model_id": "qwen3.6-plus",
                "execution_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            },
        }

    async def _fake_direct_stream(display_model, provider, metadata, system_prompt, messages, tools=None, session_id=None):
        calls.append((display_model, provider, metadata.get("execution_model_id")))
        assert system_prompt
        assert messages[-1]["role"] == "user"
        assert session_id is None
        yield {"type": "done", "model": display_model, "cost": "0", "input_tokens": 1, "output_tokens": 1}

    async def _unexpected_stream(*_args, **_kwargs):
        raise AssertionError("LiteLLM fallback path should not run for dynamic qwen models")
        yield

    async def _fake_claude_slots():
        return {}

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _fake_registry_row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", _fake_claude_slots)
    monkeypatch.setattr(model_selector, "_stream_direct_openai_provider", _fake_direct_stream)
    monkeypatch.setattr(model_selector, "_stream_litellm_openai", _unexpected_stream)
    monkeypatch.setattr(model_selector, "_stream_litellm", _unexpected_stream)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="casual", model="qwen3.6-plus", use_tools=False, tool_group=""),
            "system prompt",
            [{"role": "user", "content": "신규 모델 테스트"}],
            model_override="qwen3.6-plus",
        )
    ]

    assert calls == [("qwen3.6-plus", "qwen", "qwen3.6-plus")]
    assert events[-1]["type"] == "done"
    assert events[-1]["model"] == "qwen3.6-plus"


@pytest.mark.asyncio
async def test_gemini_route_forwards_session_id_and_active_project(monkeypatch):
    captured = {}

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"gemini-2.5-flash"}

    async def _fake_registry_row(_model_id: str, provider=None):
        return None

    async def _fake_resolve_project(_session_id):
        return "NTV2"

    async def _fake_litellm(model, system_prompt, messages, tools=None, session_id=None):
        captured["model"] = model
        captured["system_prompt"] = system_prompt
        captured["session_id"] = session_id
        yield {"type": "done", "model": model, "cost": "0", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _fake_registry_row)
    monkeypatch.setattr(model_selector, "_resolve_codex_project", _fake_resolve_project)
    monkeypatch.setattr(model_selector, "_stream_litellm", _fake_litellm)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="code_modify", model="gemini-2.5-flash", use_tools=True, tool_group="all"),
            "system prompt",
            [{"role": "user", "content": "NTV2 배포 상태 확인"}],
            tools=[{"name": "run_remote_command", "input_schema": {"type": "object", "properties": {}}}],
            model_override="gemini-2.5-flash",
            session_id="session-ntv2",
        )
    ]

    assert captured["model"] == "gemini-2.5-flash"
    assert captured["session_id"] == "session-ntv2"
    assert "project=NTV2" in captured["system_prompt"]
    assert "commit_push_deploy_ssh_docker_allowed_when_user_requests" in captured["system_prompt"]
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_dashboard_intent_no_longer_downgrades_to_haiku(monkeypatch):
    routed_models, events = await _collect_claude_route(
        monkeypatch,
        intent="dashboard",
        model="claude-sonnet",
        use_tools=True,
        tool_group="all",
    )

    assert routed_models == ["claude-sonnet"]
    assert events[-1]["model"] == "claude-sonnet"


@pytest.mark.asyncio
async def test_casual_intent_still_downgrades_to_haiku(monkeypatch):
    routed_models, events = await _collect_claude_route(
        monkeypatch,
        intent="casual",
        model="claude-sonnet",
        use_tools=False,
        tool_group="",
    )

    assert routed_models == ["claude-haiku"]
    assert events[-1]["model"] == "claude-haiku"


def test_route_metadata_accepts_json_string():
    metadata = model_selector._route_metadata(
        {
            "metadata": json.dumps(
                {
                    "execution_backend": "openai_compatible_direct",
                    "execution_model_id": "qwen3.6-plus",
                    "execution_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                }
            )
        }
    )

    assert metadata["execution_backend"] == "openai_compatible_direct"
    assert metadata["execution_model_id"] == "qwen3.6-plus"


def test_is_codex_retryable_error_distinguishes_transient_and_auth_errors():
    assert model_selector._is_codex_retryable_error("Codex Relay timeout (300s)")
    assert model_selector._is_codex_retryable_error("Codex Relay not healthy: 503")
    assert not model_selector._is_codex_retryable_error("Codex Relay 401: unauthorized")


@pytest.mark.asyncio
async def test_stream_codex_relay_retries_same_model_before_returning_done(monkeypatch):
    attempts = []

    async def _fake_stream_once(model, system_prompt, messages, tools=None, session_id=None):
        attempts.append(messages)
        if len(attempts) == 1:
            yield {"type": "delta", "content": "초안 일부"}
            yield {"type": "error", "content": "Codex Relay timeout (300s)"}
            return
        yield {"type": "delta", "content": " 이어서 마무리"}
        yield {"type": "done", "model": "GPT-5.4 (Codex CLI)", "cost": "0", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(model_selector, "_stream_codex_relay_once", _fake_stream_once)
    monkeypatch.setattr(model_selector, "_CODEX_RETRY_DELAYS", (0.0,))

    events = [
        event
        async for event in model_selector._stream_codex_relay(
            "gpt-5.4",
            "system prompt",
            [{"role": "user", "content": "계속 진행해"}],
            session_id="session-1",
        )
    ]

    assert events[0]["type"] == "model_info"
    assert any("동일 모델로 다시 이어갑니다" in event.get("content", "") for event in events if event.get("type") == "delta")
    assert events[-1]["type"] == "done"
    assert len(attempts) == 2
    assert attempts[1][-1]["role"] == "user"
    assert "직전 Codex 응답이 연결 문제로 중단되었습니다" in attempts[1][-1]["content"]
