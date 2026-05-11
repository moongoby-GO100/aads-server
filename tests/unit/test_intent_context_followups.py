from app.services.intent_router import (
    _build_intent_cache_key,
    _pc_agent_followup_override,
    _keyword_fallback,
)


def test_context_dependent_intent_cache_includes_recent_messages():
    message = "이거 확인해봐"
    first_context = [
        {"role": "assistant", "content": "NTV2 VVIC 스크래핑 구현을 점검했습니다."},
        {"role": "user", "content": message},
    ]
    second_context = [
        {"role": "assistant", "content": "AADS 프롬프트 provenance를 점검했습니다."},
        {"role": "user", "content": message},
    ]

    assert _build_intent_cache_key(message, "NTV2", first_context) != _build_intent_cache_key(
        message,
        "NTV2",
        second_context,
    )


def test_intent_cache_key_includes_workspace():
    message = "최근 작업 확인해"

    assert _build_intent_cache_key(message, "AADS", None) != _build_intent_cache_key(
        message,
        "NTV2",
        None,
    )


def test_pc_agent_followup_is_verification_not_workspace_switch():
    assert _pc_agent_followup_override("PC에이전트로 진행하라고 했는데") == "cto_verify"
    assert _pc_agent_followup_override("내 PC연결되어 있는데") == "cto_verify"


def test_pc_agent_followup_keyword_fallback_not_workspace_switch():
    override = _pc_agent_followup_override("PC에이전트로 진행하라고 했는데")
    result = _keyword_fallback("PC에이전트로 진행하라고 했는데")

    assert override == "cto_verify"
    assert result.intent != "workspace_switch"
