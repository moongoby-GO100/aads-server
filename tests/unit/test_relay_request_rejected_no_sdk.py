"""릴레이가 요청 자체를 4xx 로 거절하면 SDK·다른 슬롯으로 재전송하지 않는다.

2026-10-03 03:48Z `CLI Relay 400: {"error": "invalid JSON"}` 직후 SDK 폴백이 같은 거대
본문으로 202초간 무응답이었다. 결정적 요청 오류는 경로를 바꿔도 같은 결과다.
"""
import asyncio

import pytest

from app.services import model_selector
from app.services.intent_router import IntentResult


def _patch_common(monkeypatch):
    async def no_db_key(*args, **kwargs):
        return ""

    async def available_models():
        return {"claude-opus", "claude-sonnet", "gpt-5.6-sol"}

    async def no_registry_row(*args, **kwargs):
        return None

    async def slot_records(**kwargs):
        return {
            "1": {"priority": 1, "key_name": "slot-one"},
            "2": {"priority": 2, "key_name": "slot-two"},
        }

    monkeypatch.setattr(model_selector, "_get_db_key", no_db_key)
    monkeypatch.setattr(model_selector, "get_available_model_ids", available_models)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", no_registry_row)
    monkeypatch.setattr(model_selector, "_get_claude_slot_records", slot_records)


@pytest.mark.parametrize("content,expected", [
    ('CLI Relay 400: {"error": "invalid JSON"}', True),
    ('CLI Relay 400: {"error": "JSON object required"}', True),
    ("CLI Relay 413: Request Entity Too Large", True),
    ("CLI Relay 413: ", True),
    ('CLI Relay 400: {"error": "unsupported_claude_model"}', False),
    ("CLI Relay 401: unauthorized", False),
    ("CLI Relay 500: invalid JSON in upstream", False),
    ("invalid JSON", False),
    ("", False),
])
def test_request_rejected_classifier(content, expected):
    assert model_selector._is_request_rejected_error(content) is expected


@pytest.mark.asyncio
async def test_relay_400_invalid_json_skips_sdk_and_other_slots(monkeypatch):
    _patch_common(monkeypatch)
    calls = []

    async def rejecting_cli(model, *args, oauth_slot=None, **kwargs):
        calls.append(("claude", model, oauth_slot))
        yield {"type": "error", "content": 'CLI Relay 400: {"error": "invalid JSON"}'}

    async def sdk(*args, **kwargs):
        calls.append(("sdk", args[0], None))
        yield {"type": "delta", "content": "must not run"}

    async def other(model, *args, **kwargs):
        calls.append(("other", model, None))
        yield {"type": "done", "model": model}

    monkeypatch.setattr(model_selector, "_stream_cli_relay", rejecting_cli)
    monkeypatch.setattr(model_selector, "_stream_agent_sdk", sdk)
    monkeypatch.setattr(model_selector, "_stream_codex_relay", other)

    events = [event async for event in model_selector.call_stream(
        IntentResult(intent="cto_strategy", model="claude-opus", use_tools=True, tool_group="all"),
        "system",
        [{"role": "user", "content": "image heavy"}],
    )]

    assert len(calls) == 1 and calls[0][0] == "claude"
    errors = [e for e in events if e.get("type") == "error"]
    assert len(errors) == 1
    assert "이미지 수/크기를 줄여" in errors[0]["content"]
    assert "invalid JSON" in errors[0]["content"]
    assert errors[0]["error_type"] == "relay_request_rejected"
    assert events[-1] is errors[0]


@pytest.mark.asyncio
async def test_sdk_fallback_first_event_cap_ends_with_error(monkeypatch):
    monkeypatch.setenv("AADS_SDK_FALLBACK_FIRST_EVENT_SEC", "0.05")

    async def hanging_sdk(model, *args, **kwargs):
        yield {"type": "model_info", "model": model}
        await asyncio.sleep(30)
        yield {"type": "delta", "content": "too late"}

    monkeypatch.setattr(model_selector, "_stream_agent_sdk", hanging_sdk)

    events = await asyncio.wait_for(
        _collect(model_selector._iter_agent_sdk_fallback("claude-opus", "s", [], None)),
        timeout=5,
    )

    assert events[-1]["type"] == "error"
    assert "stream_stall_timeout" in events[-1]["content"]
    assert not any(e.get("type") == "delta" for e in events)


async def _collect(gen):
    return [event async for event in gen]


def test_sdk_first_event_cap_env_default_and_disable(monkeypatch):
    monkeypatch.delenv("AADS_SDK_FALLBACK_FIRST_EVENT_SEC", raising=False)
    assert model_selector._sdk_first_event_timeout_sec() == 60.0
    monkeypatch.setenv("AADS_SDK_FALLBACK_FIRST_EVENT_SEC", "0")
    assert model_selector._sdk_first_event_timeout_sec() is None
    monkeypatch.setenv("AADS_SDK_FALLBACK_FIRST_EVENT_SEC", "bad")
    assert model_selector._sdk_first_event_timeout_sec() == 60.0
