"""턴 모델 계약 — 요청 모델은 턴 시작에 한 번 정하고 이후 경로는 읽기만 한다.

2026-10-02 하루에 모델 바꿔치기 수정이 5건 들어갔다(6개 결정 지점이 서로 덮어씀).
행렬: {Opus 5.5, codex gpt-6.1-sol} x {정상, 인터럽트, 재개, 자동응답, 401 인증 실패, 정체}.
기대: 요청 모델 == 실행 모델. 사용자 고정 모델이 최종 실패하면 전환 없이 분류된 오류.
전환과 fallback_chain 은 user_pinned=false 일 때만 생긴다.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from app.services import chat_service, model_selector
from app.services import turn_model_contract as tmc
from app.services.intent_router import IntentResult

OPUS = "claude-opus-5-5"
SOL = "codex:gpt-6.1-sol"
MODELS = [OPUS, SOL]


def _pinned(model):
    return tmc.build_turn_contract(
        model_override=model, intent_override=None, intent_model="claude-sonnet", operational_default=None,
    )


def _auto_reaction(default_model):
    return tmc.build_turn_contract(
        model_override=None, intent_override="auto_reaction", intent_model="claude-sonnet",
        operational_default=default_model,
    )


def _attempts(contract, model_override, fallbacks):
    """send_message_stream 재시도 루프와 같은 함수로 3회 시도의 (override, 실제 모델, 재시도 지정) 를 만든다."""
    base = contract.requested_model
    return [chat_service._plan_attempt_model(model_override, base, fallbacks, n) for n in range(3)]


# ── 계약 생성 ────────────────────────────────────────────────────────────

def test_auto_selection_tokens_match_selector_and_chat_service():
    assert set(tmc.AUTO_ROUTED_DB_DEFAULT_MODELS) == set(chat_service._AUTO_ROUTED_DB_DEFAULT_MODELS)
    assert set(tmc.AUTO_ROUTED_DB_DEFAULT_MODELS) == set(model_selector._AUTO_ROUTED_DB_DEFAULT_MODELS)


@pytest.mark.parametrize("model", MODELS)
def test_selected_model_is_pinned_and_recorded_as_requested(model):
    c = _pinned(model)
    assert (c.requested_model, c.source, c.user_pinned) == (model, tmc.SOURCE_USER_SELECT, True)
    assert c.policy_exempt is True
    assert c.fallback_chain == []


@pytest.mark.parametrize("token", ["", "auto", "mixture", "auto-default-llm", "qwen-turbo", None])
def test_auto_tokens_are_not_a_user_choice_and_never_become_requested_model(token):
    c = tmc.build_turn_contract(
        model_override=token, intent_override=None, intent_model="claude-sonnet", operational_default=OPUS,
    )
    assert c.user_pinned is False
    assert c.requested_model == OPUS
    assert c.source == tmc.SOURCE_AUTO_ROUTED
    assert c.policy_exempt is False


@pytest.mark.parametrize("model", MODELS)
def test_auto_reaction_uses_operational_default_and_is_policy_exempt(model):
    c = _auto_reaction(model)
    assert (c.requested_model, c.source, c.user_pinned) == (model, tmc.SOURCE_AUTO_REACTION_DEFAULT, False)
    assert c.policy_exempt is True


def test_auto_reaction_without_db_default_falls_back_to_intent_model():
    c = tmc.build_turn_contract(
        model_override=None, intent_override="auto_reaction", intent_model="claude-sonnet", operational_default=None,
    )
    assert c.requested_model == "claude-sonnet"


def test_apply_to_intent_result_only_attaches_contract():
    c = _pinned(OPUS)
    ir = IntentResult(intent="execute", model="claude-opus", use_tools=True, tool_group="all")
    c.apply_to_intent_result(ir)
    assert ir.turn_model_contract is c and ir.policy_exempt is True
    assert ir.model == "claude-opus"


# ── 선택창 저장값 정규화 (32745da4 흡수) ─────────────────────────────────

@pytest.mark.asyncio
async def test_openai_selection_is_normalized_to_codex_when_verified_row_exists(monkeypatch):
    seen = []

    async def _prefer(provider, model_id):
        seen.append((provider, model_id))
        return "codex"

    monkeypatch.setattr(model_selector, "_prefer_codex_for_openai_pinned", _prefer)
    assert await tmc.normalize_selected_model("openai:gpt-6.1-sol") == "codex:gpt-6.1-sol"
    assert seen == [("openai", "gpt-6.1-sol")]


@pytest.mark.asyncio
async def test_openai_selection_kept_without_runnable_codex_row(monkeypatch):
    async def _prefer(provider, model_id):
        return provider

    monkeypatch.setattr(model_selector, "_prefer_codex_for_openai_pinned", _prefer)
    assert await tmc.normalize_selected_model("openai:gpt-6.1-sol") == "openai:gpt-6.1-sol"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [OPUS, SOL, "gpt-6.1-sol", "anthropic:claude-opus-5-5", "mixture"])
async def test_non_openai_selection_is_untouched_and_does_not_hit_registry(monkeypatch, value):
    async def _boom(*_a, **_k):
        raise AssertionError("registry must not be consulted")

    monkeypatch.setattr(model_selector, "_prefer_codex_for_openai_pinned", _boom)
    assert await tmc.normalize_selected_model(value) == value


@pytest.mark.asyncio
async def test_normalization_failure_keeps_original_value(monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(model_selector, "_prefer_codex_for_openai_pinned", _boom)
    assert await tmc.normalize_selected_model("openai:gpt-6.1-sol") == "openai:gpt-6.1-sol"


# ── 행렬: 정상 ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_normal_pinned_turn_runs_requested_model_on_every_attempt(model):
    c = _pinned(model)
    fallbacks = await chat_service._turn_retry_fallback_models(c)
    assert fallbacks == []
    for override, effective, is_retry in _attempts(c, model, fallbacks):
        assert override == model and effective == model and is_retry is False


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_normal_auto_turn_executes_requested_default_not_the_auto_token(model):
    c = tmc.build_turn_contract(
        model_override="auto-default-llm", intent_override=None, intent_model="claude-sonnet",
        operational_default=model,
    )
    for _override, effective, _retry in _attempts(c, "auto-default-llm", []):
        assert effective == model


# ── 행렬: 인터럽트 ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model", MODELS)
def test_interrupt_break_leaves_contract_untouched(model):
    c = _pinned(model)
    override, effective, is_retry = chat_service._plan_attempt_model(model, c.requested_model, [], 0)
    assert effective == model and is_retry is False
    assert c.fallback_chain == []
    src = inspect.getsource(chat_service.send_message_stream)
    # 5cf57465: 인터럽트로 끊은 것은 실패가 아니라 다음 시도(예비 모델)로 넘어가지 않는다.
    assert src.index("if _interrupt_stream_break:") < src.index("_deferred_interrupt_pass")


# ── 행렬: 재개 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("model", MODELS)
def test_resume_keeps_origin_requested_model_and_ignores_override_for_pinned(model):
    c = tmc.build_resume_contract(
        resolved_model="gpt-5.6-sol", origin_requested_model=model, origin_user_pinned=True,
    )
    assert (c.requested_model, c.source, c.user_pinned) == (model, tmc.SOURCE_RESUME_ORIGIN, True)
    assert c.fallback_chain == []
    assert c.policy_exempt is True


@pytest.mark.parametrize("model", MODELS)
def test_resume_of_unpinned_turn_records_override_switch(model):
    c = tmc.build_resume_contract(
        resolved_model="claude-fable-5-1", origin_requested_model=model, origin_user_pinned=False,
        override_applied="claude-fable-5-1",
    )
    assert c.requested_model == model and c.user_pinned is False
    assert [(e["from"], e["to"], e["kind"]) for e in c.fallback_chain] == [(model, "claude-fable-5-1", "resume_override")]


@pytest.mark.parametrize("model", MODELS)
def test_resume_without_origin_record_uses_session_fallback_and_says_so(model):
    c = tmc.build_resume_contract(
        resolved_model=model, origin_requested_model=None, origin_user_pinned=False, fallback_tier="session_current",
    )
    assert (c.requested_model, c.source) == (model, tmc.SOURCE_SESSION_FALLBACK)
    assert len(c.fallback_chain) == 1
    entry = c.fallback_chain[0]
    assert entry["kind"] == "session_fallback" and entry["to"] == model
    assert "origin_requested_model_missing:session_current" == entry["reason"]


def test_resume_single_stream_wires_contract_and_keeps_origin_first():
    src = inspect.getsource(chat_service._resume_single_stream)
    assert "build_resume_contract(" in src
    assert "resume_override_ignored_pinned" in src
    assert "fallback_info=_resume_contract.fallback_chain" in src
    assert src.index("resume_model_from_execution") < src.index("resume_model_from_session_current")
    # 고정 턴 이어쓰기는 같은 모델로만 재시도한다.
    assert "[_resume_model] if _resume_contract.user_pinned" in src


# ── 행렬: 자동응답 ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_auto_reaction_runs_operational_default_and_switches_only_with_record(model):
    c = _auto_reaction(model)
    fallbacks = await chat_service._turn_retry_fallback_models(c)
    plan = _attempts(c, None, fallbacks)
    assert plan[0][1] == model and plan[0][2] is False
    # 비고정 턴만 재시도가 모델을 바꿀 수 있고, 바꾸면 chain 과 배너가 같이 남는다.
    prev = model
    for _override, effective, is_retry in plan[1:]:
        banner = chat_service._note_retry_model_switch(
            c.fallback_chain, prev_model=prev, new_model=effective, base_model=model, reason="한도 초과",
        )
        if effective != prev:
            assert is_retry is True
            assert banner and model in banner and effective in banner
        prev = effective
    switched = list(c.fallback_chain)
    assert all({"from", "to", "reason", "at"} <= set(e) for e in switched)
    if fallbacks:
        assert switched and switched[0]["from"] == model


# ── 행렬: 401 인증 실패 / 정체 ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("reason", ["인증 오류", "Codex 인증 실패(재로그인 필요)", "시간 초과", "한도 초과"])
async def test_pinned_failure_never_switches_and_reports_classified_error(model, reason):
    c = _pinned(model)
    fallbacks = await chat_service._turn_retry_fallback_models(c)
    assert fallbacks == []
    for override, effective, is_retry in _attempts(c, model, fallbacks):
        assert (override, effective, is_retry) == (model, model, False)
    for n in range(1, 3):
        banner = chat_service._note_retry_model_switch(
            c.fallback_chain, prev_model=model, new_model=_attempts(c, model, fallbacks)[n][1],
            base_model=model, reason=reason,
        )
        assert banner == ""
    assert c.fallback_chain == []
    notice = tmc.pinned_failure_notice(model, reason, retried=True)
    assert model in notice and reason in notice and "바꾸지 않았습니다" in notice
    assert "재시도" in notice


def test_pinned_notice_for_auth_says_no_retry():
    notice = tmc.pinned_failure_notice(SOL, "인증 오류", retried=False)
    assert "재시도하지 않습니다" in notice


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_unpinned_failure_switches_with_chain_banner_and_actual_model(monkeypatch, model):
    c = _auto_reaction(model)

    async def _registered(chain, base):
        return chain

    monkeypatch.setattr(chat_service, "_drop_unregistered_codex_retry_candidates", _registered)
    fallbacks = await chat_service._turn_retry_fallback_models(c)
    assert fallbacks and model not in fallbacks
    override, effective, is_retry = chat_service._plan_attempt_model(None, model, fallbacks, 1)
    assert is_retry is True and effective == fallbacks[0] == override
    banner = chat_service._note_retry_model_switch(
        c.fallback_chain, prev_model=model, new_model=effective, base_model=model,
        reason=chat_service._safe_fallback_reason("401 Unauthorized"),
    )
    assert banner == chat_service._model_switch_banner(model, effective)
    assert c.fallback_chain[0]["from"] == model and c.fallback_chain[0]["to"] == effective
    assert c.fallback_chain[0]["reason"] == "인증 오류"


def test_retry_loop_gates_terminal_errors_on_user_pinned():
    src = inspect.getsource(chat_service.send_message_stream)
    assert "_turn_retry_fallback_models(_turn_contract)" in src
    assert "_plan_attempt_model(" in src
    assert "pinned_failure_notice(" in src
    assert "pinned_turn_failed_no_switch" in src
    # 5cf57465 / 62feede6 는 그대로: 인터럽트 중단 처리와 정체 가드.
    assert "_interrupt_stream_break" in src and "iter_with_stall_timeout(" in src and "StreamStallError" in src
    assert "_retry_fallback_chain: list = _turn_contract.fallback_chain" in src


def test_requested_model_is_recorded_once_from_contract():
    src = inspect.getsource(chat_service.send_message_stream)
    assert "build_turn_contract(" in src
    assert "_turn_contract.requested_model,\n" in src
    # 예전 지점: 선택창 값/auto 토큰을 그대로 requested_model 로 기록하던 UPDATE.
    assert "model_override or intent_result.model,\n                    )" not in src


# ── call_stream: 정책 강등 면제 / 강등 기록 / 고정 모델 무전환 ───────────

class _Stop(Exception):
    pass


def _patch_call_stream_preamble(monkeypatch, governed):
    async def _byok(_sid):
        return {"exempt": True, "providers": [], "user_id": "", "role": ""}

    async def _no_key(*_a, **_k):
        return ""

    async def _alias(model, provider=None):
        return model, {"provider": "anthropic", "model_id": model, "is_active": True, "is_executable": True}

    async def _temperature(_intent):
        return 0.3

    async def _stop(*_a, **_k):
        raise _Stop()

    monkeypatch.setattr(model_selector, "_byok_owner_context", _byok)
    monkeypatch.setattr(model_selector, "_get_db_key", _no_key)
    monkeypatch.setattr(model_selector, "_resolve_registered_model_alias", _alias)
    monkeypatch.setattr(model_selector, "_resolve_governed_intent_model", governed)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _stop)
    import app.services.intent_router as ir

    monkeypatch.setattr(ir, "resolve_intent_temperature", _temperature)


async def _run_until_stop(intent_result):
    try:
        async for _ in model_selector.call_stream(
            intent_result, "system", [{"role": "user", "content": "x"}], model_override=None,
        ):
            pass
    except _Stop:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_auto_reaction_default_is_not_downgraded_by_intent_policy(monkeypatch, model):
    calls = []

    async def _governed(**kwargs):
        calls.append(kwargs)
        return "claude-sonnet-5-5", "db policy allowed_models"

    _patch_call_stream_preamble(monkeypatch, _governed)
    c = _auto_reaction(model)
    ir = IntentResult(intent="execute", model=model, use_tools=True, tool_group="all")
    c.apply_to_intent_result(ir)
    await _run_until_stop(ir)
    assert calls == [], "계약이 정한 자동응답 기본 모델은 intent 정책이 건드리지 않는다"
    assert c.fallback_chain == []


@pytest.mark.asyncio
async def test_policy_downgrade_of_unpinned_auto_turn_is_recorded_with_reason(monkeypatch, caplog):
    async def _governed(**kwargs):
        return "claude-sonnet-5-5", "db policy allowed_models"

    _patch_call_stream_preamble(monkeypatch, _governed)
    c = tmc.build_turn_contract(
        model_override="auto-default-llm", intent_override=None, intent_model="claude-sonnet", operational_default=OPUS,
    )
    ir = IntentResult(intent="execute", model=OPUS, use_tools=True, tool_group="all")
    c.apply_to_intent_result(ir)
    with caplog.at_level("INFO"):
        await _run_until_stop(ir)
    assert len(c.fallback_chain) == 1
    entry = c.fallback_chain[0]
    assert (entry["from"], entry["to"], entry["kind"]) == (OPUS, "claude-sonnet-5-5", "policy_downgrade")
    assert "execute" in entry["reason"] and "db policy allowed_models" in entry["reason"]
    assert any("cascade_downgrade" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_call_stream_without_contract_keeps_legacy_policy_behavior(monkeypatch):
    calls = []

    async def _governed(**kwargs):
        calls.append(kwargs)
        return None, None

    _patch_call_stream_preamble(monkeypatch, _governed)
    ir = IntentResult(intent="execute", model=OPUS, use_tools=True, tool_group="all")
    await _run_until_stop(ir)
    assert len(calls) == 1


def test_call_stream_blocks_inner_switches_for_pinned_turns():
    src = inspect.getsource(model_selector.call_stream)
    assert "_pinned_no_switch" in src
    assert "and not _pinned_no_switch" in src  # Claude 최후 sonnet 강등
    assert "codex_pinned_no_switch" in src      # Codex -> Fable 전환


@pytest.fixture
def codex_401_harness(monkeypatch):
    """등록된 codex_cli 행이 401 을 내는 상황. 다른 모델로 넘어간 호출은 switched 에 쌓인다."""
    switched = []

    async def _no_key(*_a, **_k):
        return ""

    async def _byok(_sid):
        return {"exempt": True, "providers": [], "user_id": "", "role": ""}

    row = {"provider": "codex", "model_id": "gpt-6.1-sol", "is_active": True, "is_executable": True,
           "metadata": {"execution_backend": "codex_cli"}}

    async def _alias(model, provider=None):
        return "gpt-6.1-sol", row

    async def _row(model_id, provider=None):
        return row

    async def _models():
        return {"gpt-6.1-sol", "claude-fable-5-1"}

    async def _temperature(_intent):
        return 0.3

    async def _quota():
        return False, ""

    async def _codex(model, *_a, **_k):
        yield {"type": "error", "content": model_selector._codex_auth_failure_message("unauthorized")}

    async def _candidates(*_a, **_k):
        return ["claude-fable-5-1"]

    async def _relay(model, *_a, **_k):
        switched.append(model)
        yield {"type": "delta", "content": "fallback"}

    async def _anthropic(_intent, model, *_a, **_k):
        switched.append(model)
        yield {"type": "delta", "content": "fallback"}

    monkeypatch.setattr(model_selector, "_byok_owner_context", _byok)
    monkeypatch.setattr(model_selector, "_get_db_key", _no_key)
    monkeypatch.setattr(model_selector, "_resolve_registered_model_alias", _alias)
    monkeypatch.setattr(model_selector, "_get_registered_model_row", _row)
    monkeypatch.setattr(model_selector, "get_available_model_ids", _models)
    monkeypatch.setattr(model_selector, "_codex_quota_exhausted", _quota)
    monkeypatch.setattr(model_selector, "_stream_codex_relay", _codex)
    monkeypatch.setattr(model_selector, "_configured_llm_fallback_candidates", _candidates)
    monkeypatch.setattr(model_selector, "_stream_cli_relay", _relay)
    monkeypatch.setattr(model_selector, "_stream_anthropic", _anthropic)
    import app.services.intent_router as ir_mod

    monkeypatch.setattr(ir_mod, "resolve_intent_temperature", _temperature)
    return switched


async def _run_codex_turn(contract, *, model_override, locked):
    ir = IntentResult(intent="execute", model="gpt-6.1-sol", use_tools=True, tool_group="all")
    if locked:
        ir.model_locked = True
    contract.apply_to_intent_result(ir)
    return await asyncio.wait_for(
        _collect(model_selector.call_stream(
            ir, "system", [{"role": "user", "content": "x"}], model_override=model_override)),
        timeout=20,
    )


async def _collect(agen):
    return [e async for e in agen]


@pytest.mark.asyncio
@pytest.mark.parametrize("override,locked", [("codex:gpt-6.1-sol", True), ("gpt-6.1-sol", True)])
async def test_pinned_codex_401_is_returned_as_error_without_switching(codex_401_harness, override, locked):
    events = await _run_codex_turn(_pinned(SOL), model_override=override, locked=locked)
    assert codex_401_harness == [], "고정 Codex 모델은 다른 모델로 바뀌면 안 된다"
    assert events[-1]["type"] == "error"
    assert "Codex 인증 실패(재로그인 필요)" in events[-1]["content"]
    assert not any(e.get("type") == "model_fallback" for e in events)


@pytest.mark.asyncio
async def test_pinned_contract_blocks_codex_fallback_even_when_override_is_unqualified_and_unlocked(codex_401_harness):
    # 계약이 고정이면 model_locked/provider 접두사가 없어도 등록 행 폴백 체인이 돌지 않는다.
    events = await _run_codex_turn(_pinned(SOL), model_override=None, locked=False)
    assert codex_401_harness == []
    assert events[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_unpinned_codex_401_still_falls_back_with_visible_notice(codex_401_harness):
    events = await _run_codex_turn(_auto_reaction(SOL), model_override=None, locked=False)
    assert codex_401_harness == ["claude-fable-5-1"]
    assert any(e.get("type") == "model_fallback" and e.get("to_model") == "claude-fable-5-1" for e in events)


# ── actual_model: 메인 모델 vs 보조 모델 ─────────────────────────────────

def test_done_note_separates_aux_models_from_actual_model(caplog):
    c = _pinned(OPUS)
    event = {
        "actual_model": "claude-opus-5-5", "model_verified": True, "model_mismatch": False,
        "used_models": ["claude-haiku-4-5-20251001", "claude-opus-5-5"],
    }
    with caplog.at_level("INFO"):
        c.note_done(event, "claude-opus-5-5")
    assert c.aux_models == ["claude-haiku-4-5-20251001"]
    assert c.requested_model == OPUS
    assert any("turn_model_aux" in r.getMessage() for r in caplog.records)


def test_done_note_warns_when_actual_differs_without_any_recorded_switch(caplog):
    c = _pinned(OPUS)
    with caplog.at_level("WARNING"):
        c.note_done({"used_models": ["claude-opus-4-8"], "model_verified": True, "model_mismatch": True},
                    "claude-opus-4-8")
    assert any("turn_model_actual_differs" in r.getMessage() for r in caplog.records)
    assert c.fallback_chain == [], "원인 불명의 차이를 전환으로 위조해 기록하지 않는다"


def test_note_switch_dedupes_identical_consecutive_entries():
    c = _auto_reaction(OPUS)
    assert c.note_switch(OPUS, "claude-sonnet-5-5", reason="r", kind="policy_downgrade")
    assert c.note_switch(OPUS, "claude-sonnet-5-5", reason="r", kind="policy_downgrade") is None
    assert c.note_switch(OPUS, OPUS, reason="r", kind="policy_downgrade") is None
    assert len(c.fallback_chain) == 1
    json.dumps(c.fallback_chain)
