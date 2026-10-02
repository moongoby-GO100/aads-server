"""상한형 라우팅 섀도(R2): live 경로 랭크는 불변이고, 섀도 전용 랭크 맵으로만 후보를 계산한다."""
from __future__ import annotations

import pytest

from app.services import model_selector
from app.services import turn_model_contract as tmc
from app.services.intent_router import IntentResult

OPUS_5_5 = "claude-opus-5-5"
OPUS_5 = "claude-opus-5"

CODE_MODIFY_POLICY = {
    "default_model": "claude-opus",
    "cascade_downgrade": False,
    "allowed_models": ["claude-fable-5-1", "claude-opus-5", "claude-sonnet-5"],
}
CASUAL_POLICY = {
    "default_model": "claude-haiku",
    "cascade_downgrade": False,
    "allowed_models": ["claude-haiku", "claude-sonnet"],
}
STATUS_CHECK_POLICY = {
    "default_model": "claude-haiku",
    "cascade_downgrade": False,
    "allowed_models": ["claude-haiku", "claude-sonnet"],
}


# ── 순수 함수 / live 랭크 불변 ──────────────────────────────────────────

def test_live_rank_maps_do_not_contain_opus_5():
    assert "claude-opus-5" not in model_selector._INTENT_POLICY_CLAUDE_RANK
    assert model_selector._INTENT_POLICY_RANK_MODEL[3] == "claude-opus"
    assert model_selector._CEILING_SHADOW_RANK == {
        **model_selector._INTENT_POLICY_CLAUDE_RANK, "claude-opus-5": 3,
    }


def test_default_cascade_resolution_is_unchanged():
    f = model_selector._resolve_intent_policy_cascade_model
    assert f(OPUS_5_5, CODE_MODIFY_POLICY) == "claude-sonnet-5-5"
    assert f(OPUS_5, CASUAL_POLICY) is None
    assert f(OPUS_5_5, STATUS_CHECK_POLICY) == "claude-sonnet"
    assert f(OPUS_5_5, CODE_MODIFY_POLICY, None) == f(OPUS_5_5, CODE_MODIFY_POLICY)


def test_shadow_candidate_uses_shadow_rank_map():
    f = model_selector._ceiling_shadow_candidate
    assert f("casual", OPUS_5, CASUAL_POLICY) == "claude-sonnet"
    # 섀도 맵에서는 opus-5 가 rank 3 허용이므로 opus-5/opus-5-5 모두 code_modify 하향 후보가 없다.
    assert f("code_modify", OPUS_5, CODE_MODIFY_POLICY) is None
    assert f("code_modify", OPUS_5_5, CODE_MODIFY_POLICY) is None
    # 같은 정책의 live 계산은 그대로 하향한다(섀도 맵 분리의 핵심).
    assert model_selector._resolve_intent_policy_cascade_model(OPUS_5_5, CODE_MODIFY_POLICY) == "claude-sonnet-5-5"
    assert f("code_modify", OPUS_5, None) is None
    assert f("", OPUS_5, CASUAL_POLICY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model,intent,policy,expected", [
    (OPUS_5_5, "code_modify", CODE_MODIFY_POLICY, "claude-sonnet-5-5"),
    (OPUS_5, "casual", CASUAL_POLICY, None),
    (OPUS_5_5, "status_check", STATUS_CHECK_POLICY, "claude-sonnet"),
])
async def test_governed_intent_model_unchanged_for_auto_routing(monkeypatch, model, intent, policy, expected):
    import app.core.feature_flags as ff

    async def _flag(key, default=True):
        return key == "intent_policies_db_primary"

    async def _policies():
        return {intent: policy}

    audits = []

    async def _audit(**kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(ff, "get_flag", _flag)
    monkeypatch.setattr(model_selector, "_load_intent_policies", _policies)
    monkeypatch.setattr(model_selector, "_append_governance_audit_log", _audit)
    got, _reason = await model_selector._resolve_governed_intent_model(intent=intent, current_model=model)
    assert got == expected
    assert [a for a in audits if a["event"] == "ceiling_shadow"] == []


# ── call_stream: 섀도 기록 / live 플래그 ───────────────────────────────

class _Stop(Exception):
    pass


def _prepare(monkeypatch, *, policies, flag_on=False, audit_raises=False):
    import app.core.feature_flags as ff
    import app.services.intent_router as ir

    audits, aliases, flag_calls = [], [], []

    async def _byok(_sid):
        return {"exempt": True, "providers": [], "user_id": "", "role": ""}

    async def _no_key(*_a, **_k):
        return ""

    async def _alias(model, provider=None):
        aliases.append(model)
        return model, {"provider": "anthropic", "model_id": model, "is_active": True, "is_executable": True}

    async def _temperature(_intent):
        return 0.3

    async def _stop(*_a, **_k):
        raise _Stop()

    async def _policies():
        return policies

    async def _audit(**kwargs):
        if audit_raises:
            raise RuntimeError("audit down")
        audits.append(kwargs)

    async def _flag(key, default=True):
        flag_calls.append(key)
        return flag_on if key == "intent_ceiling_routing_live" else default

    monkeypatch.setattr(model_selector, "_byok_owner_context", _byok)
    monkeypatch.setattr(model_selector, "_get_db_key", _no_key)
    monkeypatch.setattr(model_selector, "_resolve_registered_model_alias", _alias)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _stop)
    monkeypatch.setattr(model_selector, "_load_intent_policies", _policies)
    monkeypatch.setattr(model_selector, "_append_governance_audit_log", _audit)
    monkeypatch.setattr(ff, "get_flag", _flag)
    monkeypatch.setattr(ir, "resolve_intent_temperature", _temperature)
    return audits, aliases, flag_calls


async def _run(intent_result, *, model_override=None, retry_override=False):
    try:
        async for _ in model_selector.call_stream(
            intent_result, "system", [{"role": "user", "content": "x"}],
            model_override=model_override, retry_override=retry_override,
        ):
            pass
    except _Stop:
        pass


def _auto_reaction_ir(model, intent):
    c = tmc.build_turn_contract(
        model_override=None, intent_override="auto_reaction", intent_model="claude-sonnet", operational_default=model,
    )
    ir = IntentResult(intent=intent, model=model, use_tools=False, tool_group="")
    c.apply_to_intent_result(ir)
    return c, ir


@pytest.mark.asyncio
async def test_exempt_turn_records_shadow_and_keeps_served_model(monkeypatch):
    audits, aliases, _ = _prepare(monkeypatch, policies={"casual": CASUAL_POLICY})
    c, ir = _auto_reaction_ir(OPUS_5, "casual")
    await _run(ir)
    shadows = [a for a in audits if a["event"] == "ceiling_shadow"]
    assert len(shadows) == 1
    assert shadows[0]["mode"] == "shadow"
    assert shadows[0]["db_result"]["selected_model"] == "claude-sonnet"
    assert shadows[0]["db_result"]["input_model"] == OPUS_5
    assert c.fallback_chain == []
    assert aliases == [OPUS_5]


@pytest.mark.asyncio
async def test_flag_off_never_applies_live_even_for_live_source(monkeypatch):
    audits, aliases, _ = _prepare(monkeypatch, policies={"status_check": STATUS_CHECK_POLICY}, flag_on=False)
    c, ir = _auto_reaction_ir(OPUS_5_5, "status_check")
    await _run(ir)
    assert len([a for a in audits if a["event"] == "ceiling_shadow"]) == 1
    assert c.fallback_chain == []
    assert aliases == [OPUS_5_5]


@pytest.mark.asyncio
async def test_flag_on_auto_reaction_default_is_downgraded_and_recorded(monkeypatch):
    audits, aliases, _ = _prepare(monkeypatch, policies={"status_check": STATUS_CHECK_POLICY}, flag_on=True)
    c, ir = _auto_reaction_ir(OPUS_5_5, "status_check")
    await _run(ir)
    assert len(c.fallback_chain) == 1
    entry = c.fallback_chain[0]
    assert (entry["from"], entry["to"], entry["kind"]) == (OPUS_5_5, "claude-sonnet", "ceiling_downgrade")
    assert aliases == [OPUS_5_5, "claude-sonnet"]
    assert len([a for a in audits if a["event"] == "ceiling_shadow"]) == 1


@pytest.mark.asyncio
async def test_flag_on_resume_origin_is_shadow_only(monkeypatch):
    audits, aliases, flag_calls = _prepare(monkeypatch, policies={"status_check": STATUS_CHECK_POLICY}, flag_on=True)
    c = tmc.build_resume_contract(
        resolved_model=OPUS_5_5, origin_requested_model=OPUS_5_5, origin_user_pinned=False,
    )
    ir = IntentResult(intent="status_check", model=OPUS_5_5, use_tools=False, tool_group="")
    c.apply_to_intent_result(ir)
    await _run(ir)
    assert c.fallback_chain == []
    assert aliases == [OPUS_5_5]
    assert "intent_ceiling_routing_live" not in flag_calls
    assert len([a for a in audits if a["event"] == "ceiling_shadow"]) == 1


@pytest.mark.asyncio
async def test_flag_on_message_level_user_selection_is_unchanged(monkeypatch):
    audits, aliases, flag_calls = _prepare(monkeypatch, policies={"status_check": STATUS_CHECK_POLICY}, flag_on=True)
    c = tmc.build_turn_contract(
        model_override=OPUS_5_5, intent_override=None, intent_model="claude-sonnet", operational_default=None,
    )
    ir = IntentResult(intent="status_check", model=OPUS_5_5, use_tools=False, tool_group="")
    c.apply_to_intent_result(ir)
    await _run(ir, model_override=OPUS_5_5)
    assert c.fallback_chain == []
    assert aliases == [OPUS_5_5]
    assert "intent_ceiling_routing_live" not in flag_calls
    assert len([a for a in audits if a["event"] == "ceiling_shadow"]) == 1


@pytest.mark.asyncio
async def test_retry_override_turn_is_not_recorded(monkeypatch):
    audits, aliases, _ = _prepare(monkeypatch, policies={"status_check": STATUS_CHECK_POLICY}, flag_on=True)
    ir = IntentResult(intent="status_check", model=OPUS_5_5, use_tools=False, tool_group="")
    await _run(ir, model_override=OPUS_5_5, retry_override=True)
    assert audits == []
    assert aliases == [OPUS_5_5]


@pytest.mark.asyncio
async def test_auto_routed_turn_does_not_record_shadow(monkeypatch):
    audits, _aliases, _ = _prepare(monkeypatch, policies={"status_check": STATUS_CHECK_POLICY})
    c = tmc.build_turn_contract(
        model_override="auto", intent_override=None, intent_model="claude-sonnet", operational_default=OPUS_5_5,
    )
    ir = IntentResult(intent="status_check", model=OPUS_5_5, use_tools=False, tool_group="")
    c.apply_to_intent_result(ir)
    await _run(ir)
    assert [a for a in audits if a["event"] == "ceiling_shadow"] == []


@pytest.mark.asyncio
async def test_audit_exception_does_not_break_the_turn(monkeypatch):
    _prepare(monkeypatch, policies={"casual": CASUAL_POLICY}, audit_raises=True)
    c, ir = _auto_reaction_ir(OPUS_5, "casual")
    await _run(ir)  # _Stop 까지 도달하면 흐름이 계속된 것이다
    assert c.fallback_chain == []
