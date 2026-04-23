from __future__ import annotations

import pytest

from app.services import model_selector
from app.services.intent_router import IntentResult


@pytest.mark.asyncio
async def test_call_stream_routes_dynamic_qwen_model_to_direct_provider(monkeypatch):
    calls: list[tuple[str, str, str]] = []

    async def _fake_get_db_key(*_args, **_kwargs):
        return ""

    async def _fake_available_models():
        return {"qwen3.6-plus"}

    async def _fake_registry_row(model_id: str):
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
