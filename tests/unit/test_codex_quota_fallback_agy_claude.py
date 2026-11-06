from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import intent_router, tenant_usage_limits
from app.services import model_selector as ms
from app.services.intent_router import IntentResult


def test_plan(monkeypatch):
    monkeypatch.delenv("AADS_CODEX_BYPASS_MODEL", raising=False)
    equivalent = ms._codex_to_claude_equivalent("gpt-5.4-mini")
    plan = ms._codex_fallback_plan(
        "gpt-5.4-mini", pinned=False, configured=[],
        defaults=["claude-haiku", "claude-sonnet"],
    )
    assert plan[:3] == ["antigravity", "claude-haiku", "claude-sonnet"]
    aliases = [ms._to_anthropic_runtime_alias(item) for item in plan]
    assert len(aliases) == len(set(aliases))
    assert plan == ["antigravity", "claude-haiku", "claude-sonnet"]
    assert ms._codex_fallback_plan(
        "gpt-5.6-sol", pinned=True, configured=["unused"], defaults=["unused"],
    ) == ["antigravity", ms._codex_to_claude_equivalent("gpt-5.6-sol")]
    plan = ms._codex_fallback_plan(
        "gpt-5.4-mini", pinned=False,
        configured=["gpt-5.4-mini", "antigravity", equivalent, equivalent],
        defaults=["unused"],
    )
    assert plan == ["antigravity", equivalent]


def stream(events, calls, label):
    async def generate(*args, **kwargs):
        calls.append(label)
        for event in events:
            if isinstance(event, Exception):
                raise event
            yield event
    return generate


@pytest.mark.asyncio
@pytest.mark.parametrize("agy", [
    [{"type": "error", "content": "quota"}],
    [],
    [{"type": "delta", "content": "discard"}, {"type": "error", "content": "failed"}],
    [{"type": "delta", "content": "discard"}, RuntimeError("failed")],
])
async def test_agy_failure_is_atomic(monkeypatch, agy):
    calls = []
    monkeypatch.setattr(ms, "_stream_antigravity_relay", stream(agy, calls, "agy"))
    monkeypatch.setattr(ms, "_stream_cli_relay", stream(
        [{"type": "delta", "content": "claude"}], calls, "claude"))
    events = [e async for e in ms._stream_codex_fallback_chain(
        "gpt-5.4-mini", "장애", ["antigravity", "claude-sonnet"], "", [])]
    assert calls == ["agy", "claude"]
    assert [e["content"] for e in events if e["type"] == "delta"] == ["claude"]
    assert events[0]["type"] == "model_fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    {"type": "delta", "content": "partial"},
    {"type": "tool_use", "tool_name": "write"},
    {"type": "tool_result", "content": "done"},
])
@pytest.mark.parametrize("failure", [
    {"type": "error", "content": "failed", "error_category": "transport"},
    RuntimeError("failed"),
])
async def test_partial_stage_stops_chain(monkeypatch, content, failure):
    calls = []
    monkeypatch.setattr(ms, "_stream_cli_relay", stream([content, failure], calls, "claude"))
    monkeypatch.setattr(ms, "_stream_litellm", stream([], calls, "next"))
    events = [e async for e in ms._stream_codex_fallback_chain(
        "gpt-5.4", "장애", ["claude-sonnet", "groq-test"], "", [])]
    assert calls == ["claude"]
    assert events[-1]["type"] == "error"
    assert events[-1]["fallback_errors"]


@pytest.fixture
def route(monkeypatch):
    monkeypatch.delenv("AADS_CODEX_PINNED_FALLBACK", raising=False)
    row = {"provider": "codex", "model_id": "gpt-5.4-mini",
           "is_active": True, "is_executable": True,
           "metadata": {"execution_backend": "codex_cli"}}
    for name, value in {
        "_byok_owner_context": {"exempt": True},
        "_get_db_key": "", "_prefer_codex_for_openai_pinned": None,
        "_resolve_registered_model_alias": ("gpt-5.4-mini", row),
        "_record_ceiling_shadow": None, "get_available_model_ids": {"gpt-5.4-mini"},
        "_resolve_codex_project": "", "_codex_quota_exhausted": (False, ""),
        "_configured_llm_fallback_candidates": [],
    }.items():
        monkeypatch.setattr(ms, name, AsyncMock(return_value=value))
    monkeypatch.setattr(intent_router, "resolve_intent_temperature", AsyncMock(return_value=0.2))
    monkeypatch.setattr(tenant_usage_limits, "check_tenant_usage_limit", AsyncMock())
    calls = []
    for name, label in [
        ("_stream_codex_relay", "codex"), ("_stream_antigravity_relay", "agy"),
        ("_stream_cli_relay", "claude"), ("_stream_litellm", "litellm"),
    ]:
        monkeypatch.setattr(ms, name, stream([], calls, label))
    return calls


async def run(pinned=True):
    intent = IntentResult(intent="code_modify", model="gpt-5.4-mini", use_tools=False, tool_group="")
    intent.turn_model_contract = SimpleNamespace(user_pinned=pinned, system_origin_intent=None)
    return [e async for e in ms.call_stream(
        intent, "", [], model_override="gpt-5.4-mini", session_id="test", tenant_id="unit-test",
    )]


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight", [True, False])
async def test_pinned_fallback_default(monkeypatch, route, preflight):
    ms._codex_quota_exhausted.return_value = (preflight, "quota")
    monkeypatch.setattr(ms, "_stream_antigravity_relay", stream(
        [{"type": "delta", "content": "agy response"}], route, "agy"))
    events = await run()
    assert route == (["agy"] if preflight else ["codex", "agy"])
    assert any(e["type"] == "model_fallback" and e["to_model"] == "antigravity" for e in events)
    assert events[-1]["content"] == "agy response"


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight", [True, False])
async def test_pinned_opt_out(monkeypatch, route, preflight):
    monkeypatch.setenv("AADS_CODEX_PINNED_FALLBACK", "0")
    ms._codex_quota_exhausted.return_value = (preflight, "quota")
    events = await run()
    assert "agy" not in route
    assert events[-1]["content"].startswith("provider=codex model=gpt-5.4-mini:")


@pytest.mark.asyncio
async def test_secondary_preserved(monkeypatch, route):
    ms._configured_llm_fallback_candidates.side_effect = [[], ["groq-test"]]
    monkeypatch.setattr(ms, "_stream_litellm", stream(
        [{"type": "delta", "content": "secondary"}], route, "secondary"))
    events = await run(pinned=False)
    assert route[-1] == "secondary"
    assert events[-1]["content"] == "secondary"
    exclusions = ms._configured_llm_fallback_candidates.call_args.kwargs["excluded_providers"]
    assert exclusions == {"codex", "anthropic", "openrouter", "qwen", "alibaba", "kimi", "minimax", "openai"}


@pytest.mark.asyncio
async def test_tool_error_no_fallback(monkeypatch, route):
    monkeypatch.setattr(ms, "_stream_codex_relay", stream(
        [{"type": "error", "content": "transport closed", "code": "original"}], route, "codex"))
    events = await run()
    assert route == ["codex"]
    assert events[-1]["error_category"] == "tool_transport_error"
    assert events[-1]["code"] == "original"


@pytest.mark.asyncio
async def test_final_error_preserves_attributes(monkeypatch, route):
    monkeypatch.setattr(ms, "_stream_codex_relay", stream(
        [{"type": "error", "content": "original", "error_category": "quota"}], route, "codex"))
    events = await run(pinned=False)
    error = events[-1]
    assert error["content"].startswith("original | agy:")
    assert error["error_category"] == "quota"
    assert error["fallback_errors"]
    assert ms._configured_llm_fallback_candidates.await_count == 2


@pytest.mark.asyncio
async def test_litellm_partial_stops_chain(monkeypatch):
    calls = []
    monkeypatch.setattr(ms, "_stream_litellm", stream(
        [{"type": "delta", "content": "partial"}, RuntimeError("lost")], calls, "litellm"))
    events = [e async for e in ms._stream_codex_fallback_chain(
        "gpt-5.4", "장애", ["groq-first", "groq-next"], "", [])]
    assert calls == ["litellm"]
    assert events[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_call_stream_partial_claude_preserves_original_and_stops(monkeypatch, route):
    monkeypatch.setattr(ms, "_stream_codex_relay", stream(
        [{"type": "error", "content": "original", "error_category": "quota"}], route, "codex"))
    monkeypatch.setattr(ms, "_stream_cli_relay", stream(
        [{"type": "delta", "content": "partial"}, RuntimeError("claude lost")], route, "claude"))
    events = await run(pinned=False)
    assert route == ["codex", "agy", "claude"]
    assert ms._configured_llm_fallback_candidates.await_count == 1
    assert events[-1]["error_category"] == "quota"
    assert "claude lost" in events[-1]["content"]
    assert events[-1]["fallback_errors"]


@pytest.mark.asyncio
async def test_codex_success_unchanged(monkeypatch, route):
    expected = [{"type": "delta", "content": "codex"}, {"type": "done"}]
    monkeypatch.setattr(ms, "_stream_codex_relay", stream(expected, route, "codex"))
    assert await run() == expected
    assert route == ["codex"]
    ms._configured_llm_fallback_candidates.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_final_error_prefix(monkeypatch, route):
    monkeypatch.setattr(ms, "_stream_codex_relay", stream(
        [{"type": "error", "content": "original quota", "error_category": "quota"}], route, "codex"))
    error = (await run())[-1]
    assert error["content"].startswith("provider=codex model=gpt-5.4-mini: original quota | agy:")
    assert error["error_category"] == "quota"
    assert error["fallback_errors"]
    assert ms._configured_llm_fallback_candidates.await_count == 1


@pytest.mark.parametrize('text,expected', [
    ('QUOTA exhausted', True), ('Usage Limit', True), ('RATE LIMIT', True),
    ('HTTP 429', True), ('Limit Reached', True), ('used=100%', True),
    ('401 unauthorized', False), ('token expired', False), ('general failure', False),
])
def test_quota_markers(text, expected):
    assert ms._codex_error_is_quota(text) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize('error,allowed', [
    ('401 unauthorized', False), ('token expired', False), ('general failure', False),
    ('usage limit reached', True), ('QUOTA exceeded', True),
])
@pytest.mark.parametrize('enabled', ['0', '1'])
async def test_pinned_error_scope(monkeypatch, route, error, allowed, enabled):
    monkeypatch.setenv('AADS_CODEX_PINNED_FALLBACK', enabled)
    monkeypatch.setattr(ms, '_stream_codex_relay', stream(
        [{'type': 'error', 'content': error, 'error_category': 'original'}], route, 'codex'))
    monkeypatch.setattr(ms, '_stream_antigravity_relay', stream(
        [{'type': 'delta', 'content': 'success'}], route, 'agy'))
    events = await run()
    if allowed and enabled == '1':
        assert route == ['codex', 'agy']
        assert events[-1]['content'] == 'success'
    else:
        assert route == ['codex']
        assert events[-1]['type'] == 'error'
        assert events[-1]['error_category'] == 'original'


@pytest.mark.asyncio
async def test_nonbuffered_done_is_success(monkeypatch):
    calls = []
    monkeypatch.setattr(ms, '_stream_cli_relay', stream([{'type': 'done'}], calls, 'claude'))
    events = [e async for e in ms._stream_codex_fallback_chain(
        'gpt-5.4', 'failure', ['claude-sonnet', 'claude-opus'], '', [])]
    assert calls == ['claude']
    assert events[-1] == {'type': 'done'}
