from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from app.services import model_selector
from app.services.intent_router import IntentResult, get_model_for_override


def test_anthropic_registry_model_ids_are_normalized_to_runtime_aliases():
    assert model_selector._to_anthropic_runtime_alias("claude-opus-5") == "claude-opus"
    assert model_selector._to_anthropic_runtime_alias("claude-opus-4-8") == "claude-opus"
    assert model_selector._to_anthropic_runtime_alias("claude-sonnet-4-6") == "claude-sonnet"
    assert model_selector._to_anthropic_runtime_alias("claude-haiku-4-5-20251001") == "claude-haiku"
    assert model_selector._to_anthropic_runtime_alias("claude-fable-5") == "claude-fable-5"
    assert model_selector._to_anthropic_runtime_alias("claude-fable-5-1") == "claude-fable-5-1"
    assert model_selector._to_anthropic_runtime_alias("claude-fable-5.1") == "claude-fable-5-1"
    assert model_selector._to_anthropic_runtime_alias("claude-fable-latest") == "claude-fable-5-1"
    assert model_selector._to_anthropic_runtime_alias("gpt-5.5") == "gpt-5.5"


def test_fable_5_1_uses_thinking_guard_and_drops_tool_choice():
    api_kwargs = {"tool_choice": {"type": "any"}}

    assert model_selector._is_anthropic_thinking_alias("claude-fable-5-1")
    assert model_selector._drop_tool_choice_for_thinking(
        api_kwargs,
        {"type": "adaptive", "display": "summarized"},
    )
    assert "tool_choice" not in api_kwargs


def test_fable_5_1_override_aliases_are_canonicalized():
    assert get_model_for_override("claude-fable-5-1") == "claude-fable-5-1"
    assert get_model_for_override("claude-fable-5.1") == "claude-fable-5-1"
    assert get_model_for_override("claude-fable-latest") == "claude-fable-5-1"
    assert get_model_for_override("anthropic:claude-fable-5-1") == "claude-fable-5-1"


def test_gpt_6_astra_is_registered_with_openai_runtime_guards():
    assert "gpt-6-astra" in model_selector._OPENAI_MODELS
    assert "gpt-6-astra" in model_selector._OPENAI_REASONING_MODELS
    assert "gpt-6-astra" in model_selector._OPENAI_NO_CUSTOM_SAMPLING_MODELS
    assert "gpt-6-astra" in model_selector._OPENAI_RESPONSES_TOOL_REQUIRED_MODELS
    assert model_selector._COST_MAP["gpt-6-astra"] == (10.0, 50.0)


def test_fable_5_1_can_cascade_down_to_allowed_claude_rank():
    assert (
        model_selector._resolve_intent_policy_cascade_model(
            "claude-fable-5-1",
            {"cascade_downgrade": True, "allowed_models": ["claude-opus"]},
        )
        == "claude-opus"
    )
    assert (
        model_selector._resolve_legacy_intent_cascade_model("claude-fable-5-1", "search")
        == "claude-sonnet"
    )


@pytest.mark.asyncio
async def test_db_primary_policy_no_change_blocks_legacy_fable_downgrade(monkeypatch):
    async def _db_primary_enabled(_flag_key: str, default: bool = False):
        return True

    async def _fake_policies():
        return {
            "casual": {
                "default_model": "codex:gpt-5.5",
                "cascade_downgrade": False,
                "allowed_models": ["claude-fable-5-1", "claude-sonnet", "claude-haiku"],
            },
            "search": {
                "default_model": "claude-sonnet",
                "cascade_downgrade": False,
                "allowed_models": ["claude-fable-5-1", "claude-sonnet"],
            },
        }

    monkeypatch.setattr(model_selector, "_load_intent_policies", _fake_policies)
    monkeypatch.setattr("app.core.feature_flags.get_flag", _db_primary_enabled)

    assert await model_selector._resolve_governed_intent_model(
        intent="casual",
        current_model="claude-fable-5-1",
    ) == (None, None)
    assert await model_selector._resolve_governed_intent_model(
        intent="search",
        current_model="claude-fable-5-1",
    ) == (None, None)


def test_cli_result_preserves_runtime_model_for_actual_model_audit():
    events = model_selector._map_cli_event(
        {
            "type": "result",
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "modelUsage": {
                "claude-opus-4-6[1m]": {
                    "inputTokens": 3,
                    "outputTokens": 5,
                    "costUSD": 0.123456,
                }
            },
        }
    )

    assert events == [
        {
            "type": "done",
            "model": "claude-opus-4-6",
            "actual_model": "claude-opus-4-6",
            "cost": "0.123456",
            "input_tokens": 3,
            "output_tokens": 5,
        }
    ]


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
async def test_explicit_fable_5_1_does_not_silently_downgrade_to_opus(monkeypatch):
    routed_models: list[str] = []

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"claude-fable-5-1", "claude-opus", "claude-sonnet"}

    async def _fake_registry_row(model_id: str, provider=None):
        if model_id in {"claude-fable-5-1", "claude-fable-5.1", "claude-fable-latest"}:
            return {
                "provider": "anthropic",
                "model_id": "claude-fable-5-1",
                "is_active": True,
                "is_executable": True,
                "metadata": {"execution_backend": "claude_cli_relay"},
                "execution_model_id": "claude-fable-5-1",
            }
        return None

    async def _fake_claude_slots():
        return {}

    async def _fake_cli_stream(target_model, *_args, **_kwargs):
        routed_models.append(target_model)
        yield {"type": "error", "content": "forced test failure"}

    async def _fake_agent_sdk(target_model, *_args, **_kwargs):
        routed_models.append(target_model)
        yield {"type": "error", "content": "forced sdk failure"}

    async def _fake_litellm(target_model, *_args, **_kwargs):
        routed_models.append(target_model)
        yield {"type": "error", "content": "forced litellm failure"}

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _fake_registry_row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", _fake_claude_slots)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", _fake_cli_stream)
    monkeypatch.setattr(model_selector, "_stream_agent_sdk", _fake_agent_sdk)
    monkeypatch.setattr(model_selector, "_stream_litellm", _fake_litellm)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="cto_strategy", model="claude-opus", use_tools=True, tool_group="all"),
            "system prompt",
            [{"role": "user", "content": "명시 선택 모델 확인"}],
            model_override="claude-fable-5.1",
        )
    ]

    assert "claude-fable-5-1" in routed_models
    assert "claude-opus" not in routed_models
    assert events[-1]["type"] == "error"


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
async def test_call_stream_uses_db_default_for_legacy_auto_qwen(monkeypatch):
    captured = {}

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_default_model():
        return "codex:gpt-5.5"

    async def _fake_available_models():
        return {"gpt-5.5"}

    async def _fake_registry_row(_model_id: str, provider=None):
        return None

    async def _fake_claude_slots():
        return {}

    async def _fake_codex_stream(model, system_prompt, messages, tools=None, session_id=None):
        captured["model"] = model
        captured["system_prompt"] = system_prompt
        yield {"type": "done", "model": model, "cost": "0", "input_tokens": 1, "output_tokens": 1}

    async def _unexpected_litellm(*_args, **_kwargs):
        raise AssertionError("legacy auto qwen should be replaced by the DB llm default")
        yield

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "_get_default_llm_model_from_db", _fake_default_model)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _fake_registry_row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", _fake_claude_slots)
    monkeypatch.setattr(model_selector, "_stream_codex_relay", _fake_codex_stream)
    monkeypatch.setattr(model_selector, "_stream_litellm_openai", _unexpected_litellm)
    monkeypatch.setattr(model_selector, "_stream_litellm", _unexpected_litellm)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="casual", model="qwen-turbo", use_tools=False, tool_group=""),
            "system prompt",
            [{"role": "user", "content": "응답 테스트"}],
        )
    ]

    assert captured["model"] == "gpt-5.5"
    assert "`gpt-5.5`" in captured["system_prompt"]
    assert events[-1]["type"] == "done"
    assert events[-1]["model"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_call_stream_uses_db_default_for_auto_default_sentinel(monkeypatch):
    captured = {}

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_default_model():
        return "codex:gpt-5.5"

    async def _fake_available_models():
        return {"gpt-5.5", "claude-haiku"}

    async def _fake_registry_row(_model_id: str, provider=None):
        return None

    async def _fake_claude_slots():
        return {}

    async def _fake_codex_stream(model, system_prompt, messages, tools=None, session_id=None):
        captured["model"] = model
        yield {"type": "done", "model": model, "cost": "0", "input_tokens": 1, "output_tokens": 1}

    async def _unexpected_claude_stream(*_args, **_kwargs):
        raise AssertionError("auto-default-llm must use the DB llm default without casual downgrade")
        yield

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "_get_default_llm_model_from_db", _fake_default_model)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _fake_registry_row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", _fake_claude_slots)
    monkeypatch.setattr(model_selector, "_stream_codex_relay", _fake_codex_stream)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", _unexpected_claude_stream)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="casual", model="auto-default-llm", use_tools=False, tool_group=""),
            "system prompt",
            [{"role": "user", "content": "응답 테스트"}],
        )
    ]

    assert captured["model"] == "gpt-5.5"
    assert events[-1]["type"] == "done"
    assert events[-1]["model"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_call_stream_executes_deepseek_compatibility_alias_as_canonical(monkeypatch):
    captured = {}

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"deepseek-v4-pro"}

    async def _fake_registry_row(_model_id: str, provider=None):
        return None

    async def _fake_litellm_openai(
        model,
        system_prompt,
        messages,
        tools=None,
        session_id=None,
        *,
        base_url=None,
        api_key=None,
        display_model=None,
        cost_model=None,
    ):
        captured["request_model"] = model
        captured["display_model"] = display_model
        captured["cost_model"] = cost_model
        assert system_prompt
        assert messages[-1]["role"] == "user"
        yield {"type": "done", "model": display_model, "cost": "0", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _fake_registry_row)
    monkeypatch.setattr(model_selector, "_stream_litellm_openai", _fake_litellm_openai)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="research", model="deepseek-reasoner", use_tools=False, tool_group=""),
            "system prompt",
            [{"role": "user", "content": "DeepSeek alias routing"}],
            model_override="deepseek-reasoner",
        )
    ]

    assert captured == {
        "request_model": "deepseek-reasoner",
        "display_model": "deepseek-reasoner",
        "cost_model": "deepseek-reasoner",
    }
    assert events[-1]["model"] == "deepseek-reasoner"


@pytest.mark.asyncio
async def test_call_stream_executes_deepseek_v4_display_model_with_litellm_runtime_alias(monkeypatch):
    captured = {}

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"deepseek-v4-pro"}

    async def _fake_registry_row(model_id: str, provider=None):
        return {
            "provider": "deepseek",
            "model_id": model_id,
            "execution_model_id": "deepseek-v4-pro",
            "metadata": {
                "execution_backend": "litellm_proxy",
                "execution_model_id": "deepseek-v4-pro",
            },
        }

    async def _fake_litellm_openai(
        model,
        system_prompt,
        messages,
        tools=None,
        session_id=None,
        *,
        base_url=None,
        api_key=None,
        display_model=None,
        cost_model=None,
    ):
        captured["request_model"] = model
        captured["display_model"] = display_model
        captured["cost_model"] = cost_model
        yield {"type": "done", "model": display_model, "cost": "0", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _fake_registry_row)
    monkeypatch.setattr(model_selector, "_stream_litellm_openai", _fake_litellm_openai)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="research", model="deepseek-v4-pro", use_tools=False, tool_group=""),
            "system prompt",
            [{"role": "user", "content": "DeepSeek V4 routing"}],
            model_override="deepseek-v4-pro",
        )
    ]

    assert captured == {
        "request_model": "deepseek-v4-pro",
        "display_model": "deepseek-v4-pro",
        "cost_model": "deepseek-v4-pro",
    }
    assert events[-1]["model"] == "deepseek-v4-pro"


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
async def test_casual_intent_still_downgrades_to_haiku_when_not_db_default(monkeypatch):
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


def test_route_metadata_accepts_pc_ollama_backend():
    metadata = model_selector._route_metadata(
        {
            "metadata": {
                "execution_backend": "pc_ollama",
                "execution_model_id": "gemma4:e4b",
            }
        }
    )

    assert metadata["execution_backend"] == "pc_ollama"
    assert metadata["execution_model_id"] == "gemma4:e4b"


@pytest.mark.asyncio
async def test_call_stream_routes_pc_ollama_backend(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"gemma4:e4b"}

    async def _fake_registry_row(model_id: str, provider=None):
        assert model_id == "gemma4:e4b"
        return {
            "provider": "pc_ollama",
            "model_id": model_id,
            "metadata": {
                "execution_backend": "pc_ollama",
                "execution_model_id": "gemma4:e4b",
                "timeout_seconds": 120,
            },
        }

    async def _fake_pc_ollama_stream(display_model, metadata, system_prompt, messages, tools=None, session_id=None):
        calls.append((display_model, metadata.get("execution_model_id")))
        assert system_prompt
        assert messages[-1]["role"] == "user"
        assert session_id == "session-pc"
        yield {"type": "delta", "content": "ok"}
        yield {"type": "done", "model": display_model, "cost": "$0", "input_tokens": 1, "output_tokens": 1}

    async def _unexpected_stream(*_args, **_kwargs):
        raise AssertionError("non-PC route should not run")
        yield

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _fake_registry_row)
    monkeypatch.setattr(model_selector, "_stream_pc_ollama_provider", _fake_pc_ollama_stream)
    monkeypatch.setattr(model_selector, "_stream_litellm_openai", _unexpected_stream)
    monkeypatch.setattr(model_selector, "_stream_litellm", _unexpected_stream)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="casual", model="gemma4:e4b", use_tools=False, tool_group=""),
            "system prompt",
            [{"role": "user", "content": "로컬 모델 테스트"}],
            model_override="gemma4:e4b",
            session_id="session-pc",
        )
    ]

    assert calls == [("gemma4:e4b", "gemma4:e4b")]
    assert events[-1]["type"] == "done"
    assert events[-1]["model"] == "gemma4:e4b"


@pytest.mark.asyncio
async def test_resolve_registered_model_alias_uses_registry_metadata(monkeypatch):
    async def _fake_registered_models(active_only=False):
        assert active_only is False
        return [
            {
                "provider": "anthropic",
                "model_id": "claude-sonnet",
                "metadata": {
                    "accepted_aliases": [
                        "claude-sonnet-4-6",
                        "claude-3-5-sonnet-20241022",
                    ]
                },
            }
        ]

    monkeypatch.setattr(model_selector, "_list_registered_models", _fake_registered_models)

    resolved_model, resolved_row = await model_selector._resolve_registered_model_alias("claude-sonnet-4-6")

    assert resolved_model == "claude-sonnet"
    assert resolved_row["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_resolve_registered_model_alias_uses_execution_model_id(monkeypatch):
    async def _fake_registered_models(active_only=False):
        assert active_only is False
        return [
            {
                "provider": "anthropic",
                "model_id": "claude-sonnet",
                "execution_model_id": "claude-sonnet-4-6",
                "metadata": {
                    "execution_backend": "claude_cli_relay",
                    "execution_model_id": "claude-sonnet-4-6",
                },
            }
        ]

    monkeypatch.setattr(model_selector, "_list_registered_models", _fake_registered_models)

    resolved_model, resolved_row = await model_selector._resolve_registered_model_alias("claude-sonnet-4-6")

    assert resolved_model == "claude-sonnet"
    assert resolved_row["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_registered_model_lookup_prefers_exact_model_id_before_alias(monkeypatch):
    async def _fake_registered_models(active_only=False):
        assert active_only is False
        return [
            {
                "provider": "deepseek",
                "model_id": "deepseek-reasoner",
                "execution_model_id": "deepseek-reasoner",
                "metadata": {
                    "canonical_model": "deepseek-v4-pro",
                    "execution_model_id": "deepseek-reasoner",
                },
            },
            {
                "provider": "deepseek",
                "model_id": "deepseek-v4-pro",
                "execution_model_id": "deepseek-v4-pro",
                "metadata": {"execution_model_id": "deepseek-v4-pro"},
            },
        ]

    monkeypatch.setattr(model_selector, "_list_registered_models", _fake_registered_models)

    row = await model_selector._get_registered_model_row("deepseek-v4-pro", provider="deepseek")

    assert row["model_id"] == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_registry_fallback_prefers_same_provider_and_family(monkeypatch):
    rows = [
        {
            "provider": "anthropic",
            "model_id": "claude-opus",
            "family": "claude",
            "category": "coding",
            "supports_tools": True,
            "supports_thinking": False,
            "supports_vision": False,
            "supports_coding": True,
            "input_cost": 5,
            "output_cost": 25,
            "is_active": False,
            "metadata": {},
        },
        {
            "provider": "anthropic",
            "model_id": "claude-sonnet",
            "family": "claude",
            "category": "coding",
            "supports_tools": True,
            "supports_thinking": False,
            "supports_vision": False,
            "supports_coding": True,
            "input_cost": 3,
            "output_cost": 15,
            "is_active": True,
            "metadata": {},
        },
        {
            "provider": "openai",
            "model_id": "gpt-5.4",
            "family": "gpt",
            "category": "coding",
            "supports_tools": True,
            "supports_thinking": False,
            "supports_vision": False,
            "supports_coding": True,
            "input_cost": 2.5,
            "output_cost": 15,
            "is_active": True,
            "metadata": {},
        },
    ]

    async def _fake_registered_models(active_only=False):
        return [row for row in rows if not active_only or row["is_active"]]

    monkeypatch.setattr(model_selector, "_list_registered_models", _fake_registered_models)

    fallback = await model_selector._fallback_for_unavailable_model(
        "claude-opus",
        {"claude-sonnet", "gpt-5.4"},
        requested_row=rows[0],
    )

    assert fallback == "claude-sonnet"


@pytest.mark.asyncio
async def test_call_stream_routes_registry_codex_backend_without_static_allowlist(monkeypatch):
    captured = {}

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"gpt-5.5-preview"}

    async def _fake_registered_models(active_only=False):
        return [
            {
                "provider": "codex",
                "model_id": "gpt-5.5-preview",
                "execution_model_id": "gpt-5.5-preview",
                "is_active": True,
                "metadata": {
                    "execution_backend": "codex_cli",
                    "execution_model_id": "gpt-5.5-preview",
                },
            }
        ]

    async def _fake_codex_stream(model, system_prompt, messages, tools=None, session_id=None):
        captured["model"] = model
        captured["system_prompt"] = system_prompt
        captured["session_id"] = session_id
        yield {"type": "done", "model": model, "cost": "0", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_list_registered_models", _fake_registered_models)
    monkeypatch.setattr(model_selector, "_stream_codex_relay", _fake_codex_stream)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="code_modify", model="gpt-5.5-preview", use_tools=True, tool_group="all"),
            "system prompt",
            [{"role": "user", "content": "codex registry routing"}],
            model_override="gpt-5.5-preview",
            session_id="session-codex-registry",
        )
    ]

    assert captured["model"] == "gpt-5.5-preview"
    assert captured["session_id"] == "session-codex-registry"
    assert events[-1]["type"] == "done"
    assert events[-1]["model"] == "gpt-5.5-preview"


@pytest.mark.asyncio
async def test_call_stream_falls_back_immediately_when_gpt_56_relay_is_busy(monkeypatch):
    calls = []

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"gpt-5.6-sol", "claude-fable-5-1", "claude-opus"}

    async def _fake_registered_models(active_only=False):
        return [
            {
                "provider": "codex",
                "model_id": "gpt-5.6-sol",
                "execution_model_id": "gpt-5.6-sol",
                "is_active": True,
                "metadata": {
                    "execution_backend": "codex_cli",
                    "execution_model_id": "gpt-5.6-sol",
                },
            }
        ]

    async def _fake_codex_stream(model, system_prompt, messages, tools=None, session_id=None):
        calls.append(("codex", model))
        yield {"type": "error", "content": "codex_relay_busy: relay_semaphore_timeout"}

    async def _fake_cli_stream(model, system_prompt, messages, tools=None, session_id=None, oauth_slot=None):
        calls.append(("claude", model))
        yield {
            "type": "done",
            "model": model,
            "cost": "0",
            "input_tokens": 1,
            "output_tokens": 1,
        }

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_list_registered_models", _fake_registered_models)
    monkeypatch.setattr(model_selector, "_stream_codex_relay", _fake_codex_stream)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", _fake_cli_stream)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="code_modify", model="gpt-5.6-sol", use_tools=True, tool_group="all"),
            "system prompt",
            [{"role": "user", "content": "relay busy fallback"}],
            model_override="gpt-5.6-sol",
            session_id="session-codex-busy",
        )
    ]

    assert calls == [("codex", "gpt-5.6-sol"), ("claude", "claude-fable-5-1")]
    assert any("claude-fable-5-1" in event.get("content", "") for event in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["model"] == "claude-fable-5-1"


@pytest.mark.asyncio
async def test_call_stream_uses_deepseek_when_gpt_56_claude_and_gemini_fail(monkeypatch):
    calls = []

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {
            "gpt-5.6-sol",
            "claude-fable-5-1",
            "claude-opus",
            "gemini-3.1-pro-preview",
            "deepseek-v4-flash",
        }

    async def _fake_registered_models(active_only=False):
        return [
            {
                "provider": "codex",
                "model_id": "gpt-5.6-sol",
                "execution_model_id": "gpt-5.6-sol",
                "is_active": True,
                "metadata": {
                    "execution_backend": "codex_cli",
                    "execution_model_id": "gpt-5.6-sol",
                },
            }
        ]

    async def _fake_codex_stream(model, system_prompt, messages, tools=None, session_id=None):
        calls.append(("codex", model))
        yield {"type": "error", "content": "codex_relay_busy: relay_semaphore_timeout"}

    async def _fake_cli_stream(model, system_prompt, messages, tools=None, session_id=None, oauth_slot=None):
        calls.append(("claude", model))
        yield {"type": "error", "content": "no OAuth token available"}

    async def _fake_litellm_stream(model, system_prompt, messages, tools=None, session_id=None):
        calls.append(("litellm", model))
        if model == "gemini-3.1-pro-preview":
            yield {"type": "error", "content": "RESOURCE_EXHAUSTED"}
            return
        yield {"type": "delta", "content": "fallback ok"}
        yield {
            "type": "done",
            "model": model,
            "cost": "0",
            "input_tokens": 1,
            "output_tokens": 1,
        }

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_list_registered_models", _fake_registered_models)
    monkeypatch.setattr(model_selector, "_stream_codex_relay", _fake_codex_stream)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", _fake_cli_stream)
    monkeypatch.setattr(model_selector, "_stream_litellm", _fake_litellm_stream)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="code_modify", model="gpt-5.6-sol", use_tools=True, tool_group="all"),
            "system prompt",
            [{"role": "user", "content": "all-provider fallback"}],
            model_override="gpt-5.6-sol",
            session_id="session-codex-all-fallbacks",
        )
    ]

    assert calls == [
        ("codex", "gpt-5.6-sol"),
        ("claude", "claude-fable-5-1"),
        ("claude", "claude-opus"),
        ("litellm", "gemini-3.1-pro-preview"),
        ("litellm", "deepseek-v4-flash"),
    ]
    assert events[-1]["type"] == "done"
    assert events[-1]["model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
async def test_call_stream_routes_registry_claude_backend_without_static_allowlist(monkeypatch):
    captured = {}

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"claude-sonnet-next"}

    async def _fake_registered_models(active_only=False):
        return [
            {
                "provider": "anthropic",
                "model_id": "claude-sonnet-next",
                "execution_model_id": "claude-sonnet-next",
                "is_active": True,
                "metadata": {
                    "execution_backend": "claude_cli_relay",
                    "execution_model_id": "claude-sonnet-next",
                },
            }
        ]

    async def _fake_claude_slots():
        return {}

    async def _fake_cli_stream(target_model, system_prompt, messages, tools=None, session_id=None, oauth_slot=None):
        captured["model"] = target_model
        captured["session_id"] = session_id
        yield {"type": "done", "model": target_model, "cost": "0", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(model_selector, "_get_db_key", _fake_get_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _fake_available_models)
    monkeypatch.setattr(model_selector, "_list_registered_models", _fake_registered_models)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", _fake_claude_slots)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", _fake_cli_stream)

    events = [
        event
        async for event in model_selector.call_stream(
            IntentResult(intent="code_modify", model="claude-sonnet-next", use_tools=True, tool_group="all"),
            "system prompt",
            [{"role": "user", "content": "claude registry routing"}],
            model_override="claude-sonnet-next",
            session_id="session-claude-registry",
        )
    ]

    assert captured["model"] == "claude-sonnet-next"
    assert captured["session_id"] == "session-claude-registry"
    assert events[-1]["type"] == "done"
    assert events[-1]["model"] == "claude-sonnet-next"


def test_is_codex_retryable_error_distinguishes_transient_and_auth_errors():
    assert model_selector._is_codex_retryable_error("Codex Relay timeout (300s)")
    assert model_selector._is_codex_retryable_error("Codex Relay not healthy: 503")
    assert model_selector._is_codex_retryable_error("Codex Relay 429: rate limit, please retry later")
    assert not model_selector._is_codex_retryable_error("Codex Relay 401: unauthorized")
    assert not model_selector._is_codex_retryable_error("relay_mcp_preflight_failed: preflight_failed")
    assert not model_selector._is_codex_retryable_error("missing_binary: codex command not found")
    assert not model_selector._is_codex_retryable_error("codex_relay_busy: relay_semaphore_timeout")
    assert not model_selector._is_codex_retryable_error("You've hit your limit · resets 3am")
    assert not model_selector._is_codex_retryable_error("You exceeded your current quota, please check your plan and billing details.")


def test_relay_retry_policy_defaults_to_three_seconds_thirty_retries():
    assert len(model_selector._CODEX_RETRY_DELAYS) == 30
    assert set(model_selector._CODEX_RETRY_DELAYS) == {3.0}
    assert len(model_selector._CLI_RETRY_DELAYS) == 30
    assert set(model_selector._CLI_RETRY_DELAYS) == {3.0}


def test_parse_quota_reset_seconds_handles_codex_cli_fixed_kst_time():
    seconds = model_selector._parse_quota_reset_seconds("You've hit your limit · resets 3am (Asia/Seoul)")

    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    expected_target = now_kst.replace(hour=3, minute=0, second=0, microsecond=0)
    if expected_target <= now_kst:
        expected_target += timedelta(days=1)
    expected_seconds = max(int((expected_target - now_kst).total_seconds()), 60)

    assert abs(seconds - expected_seconds) <= 2


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
    retry_events = [event for event in events if event.get("type") == "retry_progress"]
    assert retry_events
    assert retry_events[0]["attempt"] == 1
    assert retry_events[0]["max_attempts"] == 1
    assert events[-1]["type"] == "done"
    assert len(attempts) == 2
    assert attempts[1][-1]["role"] == "user"
    assert "직전 Codex 응답이 연결 문제로 중단되었습니다" in attempts[1][-1]["content"]


@pytest.mark.asyncio
async def test_stream_cli_relay_retries_same_model_before_returning_done(monkeypatch):
    attempts = []

    async def _fake_stream_once(model, system_prompt, messages, tools=None, session_id=None, oauth_slot=None):
        attempts.append(messages)
        if len(attempts) == 1:
            yield {"type": "delta", "content": "Claude 초안 일부"}
            yield {"type": "error", "content": "CLI Relay timeout (600s)"}
            return
        yield {"type": "delta", "content": " 이어서 마무리"}
        yield {"type": "done", "model": "claude-sonnet", "cost": "0", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(model_selector, "_stream_cli_relay_once", _fake_stream_once)
    monkeypatch.setattr(model_selector, "_CLI_RETRY_DELAYS", (0.0,))

    events = [
        event
        async for event in model_selector._stream_cli_relay(
            "claude-sonnet",
            "system prompt",
            [{"role": "user", "content": "계속 진행해"}],
            session_id="session-1",
        )
    ]

    retry_events = [event for event in events if event.get("type") == "retry_progress"]
    assert retry_events
    assert retry_events[0]["attempt"] == 1
    assert retry_events[0]["max_attempts"] == 1
    assert events[-1]["type"] == "done"
    assert len(attempts) == 2
    assert attempts[1][-1]["role"] == "user"
    assert "직전 Claude CLI 응답이 연결 문제로 중단되었습니다" in attempts[1][-1]["content"]
