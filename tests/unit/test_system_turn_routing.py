"""시스템 발신 턴(runner_response·system_trigger) 모델 분리 — 섀도 항상 ON, live 는 플래그·intent 목록 한정."""
from __future__ import annotations

import pytest

from app.services import model_selector
from app.services import turn_model_contract as tmc
from app.services.intent_router import IntentResult

OPUS_5_5 = "claude-opus-5-5"
SONNET_5 = "claude-sonnet-5"

RUNNER_RESPONSE_POLICY = {
    "default_model": SONNET_5,
    "cascade_downgrade": False,
    "allowed_models": [SONNET_5, "claude-haiku"],
}
SYSTEM_TRIGGER_POLICY = {
    "default_model": SONNET_5,
    "cascade_downgrade": False,
    "allowed_models": [SONNET_5, "claude-opus"],
}


class _Stop(Exception):
    pass


def _prepare(monkeypatch, *, policies, flag_on=False, non_executable=(), audit_raises=False, env=None):
    import app.core.feature_flags as ff
    import app.services.intent_router as ir

    audits, aliases, flag_calls = [], [], []

    async def _byok(_sid):
        return {"exempt": True, "providers": [], "user_id": "", "role": ""}

    async def _no_key(*_a, **_k):
        return ""

    async def _alias(model, provider=None):
        aliases.append(model)
        return model, {
            "provider": "anthropic", "model_id": model, "is_active": True,
            "is_executable": model not in non_executable,
        }

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
        return flag_on if key == "system_turn_routing_live" else default

    monkeypatch.setattr(model_selector, "_byok_owner_context", _byok)
    monkeypatch.setattr(model_selector, "_get_db_key", _no_key)
    monkeypatch.setattr(model_selector, "_resolve_registered_model_alias", _alias)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _stop)
    monkeypatch.setattr(model_selector, "_load_intent_policies", _policies)
    monkeypatch.setattr(model_selector, "_append_governance_audit_log", _audit)
    monkeypatch.setattr(ff, "get_flag", _flag)
    monkeypatch.setattr(ir, "resolve_intent_temperature", _temperature)
    if env is None:
        monkeypatch.delenv("SYSTEM_TURN_ROUTING_INTENTS", raising=False)
    else:
        monkeypatch.setenv("SYSTEM_TURN_ROUTING_INTENTS", env)
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


def _system_ir(intent_override, model=OPUS_5_5, classified="status_check", response_mode="fast"):
    c = tmc.build_turn_contract(
        model_override=None, intent_override=intent_override, intent_model="claude-sonnet", operational_default=model,
    )
    c.response_mode = response_mode
    ir = IntentResult(intent=classified, model=model, use_tools=False, tool_group="")
    c.apply_to_intent_result(ir)
    return c, ir


def _shadows(audits):
    return [a for a in audits if a["event"] == "system_turn_shadow"]


# ── 계약 ────────────────────────────────────────────────────────────────

def test_contract_marks_system_origin_turns():
    c = tmc.build_turn_contract(
        model_override=None, intent_override="system_trigger", intent_model="claude-sonnet", operational_default=OPUS_5_5,
    )
    assert c.source == tmc.SOURCE_SYSTEM_ORIGIN
    assert (c.system_origin_intent, c.intent_override, c.user_pinned) == ("system_trigger", "system_trigger", False)
    # 서빙 동작은 auto_routed 와 같다: 정책 면제가 아니다.
    assert c.policy_exempt is False

    r = tmc.build_turn_contract(
        model_override="auto", intent_override="auto_reaction", intent_model="claude-sonnet", operational_default=OPUS_5_5,
    )
    assert r.source == tmc.SOURCE_AUTO_REACTION_DEFAULT
    assert (r.system_origin_intent, r.intent_override) == ("runner_response", "auto_reaction")
    assert r.policy_exempt is True


def test_user_selected_model_is_never_system_origin():
    c = tmc.build_turn_contract(
        model_override=OPUS_5_5, intent_override="system_trigger", intent_model="claude-sonnet", operational_default=None,
    )
    assert (c.source, c.user_pinned, c.system_origin_intent) == (tmc.SOURCE_USER_SELECT, True, None)


def test_ordinary_turn_has_no_system_origin():
    c = tmc.build_turn_contract(
        model_override=None, intent_override=None, intent_model="claude-sonnet", operational_default=OPUS_5_5,
    )
    assert (c.source, c.system_origin_intent, c.intent_override) == (tmc.SOURCE_AUTO_ROUTED, None, None)


# ── (a) 플래그 OFF: 서빙 불변 + 섀도 1행 ─────────────────────────────────

@pytest.mark.asyncio
async def test_flag_off_keeps_served_model_and_records_one_shadow(monkeypatch):
    audits, aliases, _ = _prepare(monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY})
    c, ir = _system_ir("auto_reaction", response_mode="quality")
    await _run(ir)
    rows = _shadows(audits)
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "shadow"
    assert row["legacy_result"]["served"] == OPUS_5_5
    assert row["db_result"] == {
        "would_select": SONNET_5, "intent": "runner_response", "intent_override": "auto_reaction",
        "response_mode": "quality", "executable": True, "applied": False, "reason": "ok",
    }
    assert c.fallback_chain == []
    assert aliases == [OPUS_5_5, SONNET_5]  # 두 번째는 후보 실행 가능 여부 조회, 서빙 모델 아님


# ── (b) ON + runner_response: intent_policies 기본 모델 적용 ─────────────

@pytest.mark.asyncio
async def test_flag_on_runner_response_applies_policy_default(monkeypatch):
    audits, aliases, _ = _prepare(monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY}, flag_on=True)
    c, ir = _system_ir("auto_reaction")
    await _run(ir)
    assert len(c.fallback_chain) == 1
    entry = c.fallback_chain[0]
    assert (entry["from"], entry["to"], entry["kind"]) == (OPUS_5_5, SONNET_5, "system_turn_route")
    assert aliases[-1] == SONNET_5
    rows = _shadows(audits)
    assert len(rows) == 1
    assert rows[0]["mode"] == "live"
    assert rows[0]["db_result"]["applied"] is True


@pytest.mark.asyncio
async def test_second_call_in_same_turn_reuses_result_and_does_not_double_record(monkeypatch):
    audits, _aliases, _ = _prepare(monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY}, flag_on=True)
    c, ir = _system_ir("auto_reaction")
    await _run(ir)
    await _run(ir)
    assert len(_shadows(audits)) == 1
    assert len(c.fallback_chain) == 1


@pytest.mark.asyncio
async def test_env_can_restrict_live_intents(monkeypatch):
    audits, aliases, _ = _prepare(
        monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY}, flag_on=True, env="runner_notification",
    )
    c, ir = _system_ir("auto_reaction")
    await _run(ir)
    assert c.fallback_chain == []
    row = _shadows(audits)[0]
    assert row["db_result"]["reason"] == "intent_not_in_live_list"
    assert row["db_result"]["applied"] is False


# ── (c) ON + system_trigger: 불변(섀도만) ───────────────────────────────

@pytest.mark.asyncio
async def test_flag_on_system_trigger_is_shadow_only(monkeypatch):
    audits, _aliases, flag_calls = _prepare(
        monkeypatch, policies={"system_trigger": SYSTEM_TRIGGER_POLICY}, flag_on=True,
    )
    c, ir = _system_ir("system_trigger", response_mode="quality")
    await _run(ir)
    assert c.fallback_chain == []
    rows = _shadows(audits)
    assert len(rows) == 1
    assert rows[0]["mode"] == "shadow"
    assert rows[0]["db_result"]["would_select"] == SONNET_5
    assert rows[0]["db_result"]["intent_override"] == "system_trigger"
    assert rows[0]["db_result"]["response_mode"] == "quality"
    assert rows[0]["db_result"]["reason"] == "intent_not_in_live_list"
    assert "system_turn_routing_live" not in flag_calls


# ── (d) 사용자 메시지 단위 모델 지정: 불변·미기록 ───────────────────────

@pytest.mark.asyncio
async def test_message_level_user_selection_is_unchanged(monkeypatch):
    audits, aliases, flag_calls = _prepare(
        monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY}, flag_on=True,
    )
    c = tmc.build_turn_contract(
        model_override=OPUS_5_5, intent_override="auto_reaction", intent_model="claude-sonnet", operational_default=None,
    )
    ir = IntentResult(intent="status_check", model=OPUS_5_5, use_tools=False, tool_group="")
    c.apply_to_intent_result(ir)
    await _run(ir, model_override=OPUS_5_5)
    assert c.fallback_chain == []
    assert aliases == [OPUS_5_5]
    assert "system_turn_routing_live" not in flag_calls
    assert _shadows(audits) == []


@pytest.mark.asyncio
async def test_retry_override_turn_is_not_recorded(monkeypatch):
    audits, aliases, _ = _prepare(monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY}, flag_on=True)
    c, ir = _system_ir("auto_reaction")
    await _run(ir, model_override=OPUS_5_5, retry_override=True)
    assert c.fallback_chain == []
    assert _shadows(audits) == []
    assert aliases == [OPUS_5_5]


@pytest.mark.asyncio
async def test_ordinary_auto_routed_turn_is_not_recorded(monkeypatch):
    audits, _aliases, _ = _prepare(monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY}, flag_on=True)
    c = tmc.build_turn_contract(
        model_override=None, intent_override=None, intent_model="claude-sonnet", operational_default=OPUS_5_5,
    )
    ir = IntentResult(intent="status_check", model=OPUS_5_5, use_tools=False, tool_group="")
    c.apply_to_intent_result(ir)
    await _run(ir)
    assert _shadows(audits) == []


# ── (e) 후보 비실행: 불변 + 사유 기록 ───────────────────────────────────

@pytest.mark.asyncio
async def test_non_executable_candidate_is_not_applied(monkeypatch):
    audits, aliases, _ = _prepare(
        monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY}, flag_on=True, non_executable=(SONNET_5,),
    )
    c, ir = _system_ir("auto_reaction")
    await _run(ir)
    assert c.fallback_chain == []
    row = _shadows(audits)[0]
    assert row["mode"] == "shadow"
    assert row["db_result"]["executable"] is False
    assert row["db_result"]["applied"] is False
    assert row["db_result"]["reason"] == "candidate_not_executable"


# ── 보조 경계 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_undefined_policy_is_recorded_as_such(monkeypatch):
    audits, _aliases, _ = _prepare(monkeypatch, policies={}, flag_on=True)
    c, ir = _system_ir("system_trigger")
    await _run(ir)
    row = _shadows(audits)[0]
    assert row["db_result"]["would_select"] is None
    assert row["db_result"]["reason"] == "policy_undefined"
    assert c.fallback_chain == []


@pytest.mark.asyncio
async def test_candidate_never_upgrades_a_cheaper_served_model(monkeypatch):
    audits, _aliases, _ = _prepare(monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY}, flag_on=True)
    c, ir = _system_ir("auto_reaction", model="claude-haiku")
    await _run(ir)
    assert c.fallback_chain == []
    assert _shadows(audits)[0]["db_result"]["reason"] == "would_upgrade"


@pytest.mark.asyncio
async def test_same_model_candidate_is_a_noop(monkeypatch):
    audits, _aliases, _ = _prepare(monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY}, flag_on=True)
    c, ir = _system_ir("auto_reaction", model=SONNET_5)
    await _run(ir)
    assert c.fallback_chain == []
    assert _shadows(audits)[0]["db_result"]["reason"] == "same_model"


@pytest.mark.asyncio
async def test_audit_exception_does_not_break_the_turn(monkeypatch):
    _prepare(monkeypatch, policies={"runner_response": RUNNER_RESPONSE_POLICY}, flag_on=True, audit_raises=True)
    c, ir = _system_ir("auto_reaction")
    await _run(ir)  # _Stop 까지 도달하면 흐름이 계속된 것이다
    assert c.fallback_chain == []


def test_live_intents_env_parsing(monkeypatch):
    monkeypatch.delenv("SYSTEM_TURN_ROUTING_INTENTS", raising=False)
    assert model_selector._system_turn_live_intents() == frozenset({"runner_response", "runner_notification"})
    monkeypatch.setenv("SYSTEM_TURN_ROUTING_INTENTS", " runner_response , ")
    assert model_selector._system_turn_live_intents() == frozenset({"runner_response"})
    monkeypatch.setenv("SYSTEM_TURN_ROUTING_INTENTS", "")
    assert model_selector._system_turn_live_intents() == frozenset()
