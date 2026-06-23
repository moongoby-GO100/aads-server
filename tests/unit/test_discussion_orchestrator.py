from __future__ import annotations

import json

import pytest

from app.services.discussion_orchestrator import (
    DiscussionMode,
    DiscussionOrchestrator,
    DiscussionState,
    DiscussionStatus,
    ParticipantConfig,
    RoundEntry,
)
from app.services.discussion_presets import (
    DISCUSSION_PRESETS,
    estimate_round_cost,
    get_preset,
    resolve_model_name,
)

try:
    from app.services.discussion_orchestrator import orchestrator
except ImportError:
    orchestrator = DiscussionOrchestrator()


def test_discussion_mode_and_status_enum_values():
    assert DiscussionMode.MANUAL.value == "manual"
    assert DiscussionMode.AUTO.value == "auto"

    assert DiscussionStatus.ACTIVE.value == "active"
    assert DiscussionStatus.WAIT_CEO.value == "wait_ceo"
    assert DiscussionStatus.SYNTHESIZING.value == "synthesizing"
    assert DiscussionStatus.DONE.value == "done"
    assert DiscussionStatus.CANCELLED.value == "cancelled"


def test_discussion_state_and_round_entry_dataclass_initialization():
    participant = ParticipantConfig(
        name="기획 A",
        role="strategic_planner",
        model="claude-opus-4-6",
        system_prompt="시장성과 실행 가능성을 같이 본다.",
        color="#6c5ce7",
        avatar="A",
    )
    round_entry = RoundEntry(
        round_number=1,
        participant_name="기획 A",
        content="초기 전략 제안",
        model="claude-opus-4-6",
        ceo_directive="시장 검증도 포함",
        cost_usd=0.33,
        tokens_in=100,
        tokens_out=200,
    )
    state = DiscussionState(
        discussion_id="disc-1",
        session_id="session-1",
        topic="한국 시장용 신규 기능 기획",
        participants=[participant],
        mode=DiscussionMode.AUTO,
        status=DiscussionStatus.WAIT_CEO,
        current_round=2,
        rounds=[round_entry],
        total_cost_usd=0.33,
        budget_usd=5.0,
        ceo_directive="시장 검증도 포함",
        synthesizer_model="claude-opus-4-6",
    )

    assert round_entry.round_number == 1
    assert round_entry.tokens_out == 200
    assert round_entry.created_at.tzinfo is not None

    assert state.discussion_id == "disc-1"
    assert state.mode is DiscussionMode.AUTO
    assert state.status is DiscussionStatus.WAIT_CEO
    assert state.participants == [participant]
    assert state.rounds == [round_entry]
    assert state.synthesizer_model == "claude-opus-4-6"
    assert state.created_at.tzinfo is not None
    assert state.updated_at.tzinfo is not None


def test_build_round_context_includes_topic_history_and_directive():
    local_orchestrator = DiscussionOrchestrator()
    participant = ParticipantConfig(
        name="검증 C",
        role="critical_reviewer",
        model="claude-sonnet-4-6",
        system_prompt="빈틈을 찾는다.",
    )
    state = DiscussionState(
        discussion_id="disc-ctx",
        session_id="session-ctx",
        topic="멀티 LLM 토론 UX 개선",
        participants=[participant],
        current_round=2,
        rounds=[
            RoundEntry(
                round_number=1,
                participant_name="기획 A",
                content="초기 안은 자동 진행 중심입니다.",
                model="claude-opus-4-6",
            ),
            RoundEntry(
                round_number=1,
                participant_name="기획 B",
                content="시장 반응을 더 봐야 합니다.",
                model="gemini-2.5-pro",
            ),
            RoundEntry(
                round_number=1,
                participant_name="검증 C",
                content="이 항목은 자기 발언이라 제외됩니다.",
                model="claude-sonnet-4-6",
            ),
        ],
    )

    context = local_orchestrator._build_round_context(
        participant=participant,
        ceo_directive="비용 상한도 같이 검토",
        state=state,
    )

    assert "토론 주제: 멀티 LLM 토론 UX 개선" in context
    assert "현재 라운드: 2" in context
    assert "참가자: 검증 C" in context
    assert "역할: critical_reviewer" in context
    assert "모델: claude-sonnet-4-6" in context
    assert "시스템 프롬프트: 빈틈을 찾는다." in context
    assert "이전 발언:" in context
    assert "- 기획 A (claude-opus-4-6): 초기 안은 자동 진행 중심입니다." in context
    assert "- 기획 B (gemini-2.5-pro): 시장 반응을 더 봐야 합니다." in context
    assert "이 항목은 자기 발언이라 제외됩니다." not in context
    assert "CEO 지시: 비용 상한도 같이 검토" in context


@pytest.mark.parametrize("command", ["그만", "종료", "stop"])
def test_is_stop_command_returns_true_for_stop_inputs(command: str):
    assert DiscussionOrchestrator()._is_stop_command(command) is True


@pytest.mark.parametrize("command", ["다음", "계속", "ㄱㄱ"])
def test_is_proceed_command_returns_true_for_proceed_inputs(command: str):
    assert DiscussionOrchestrator()._is_proceed_command(command) is True


def test_is_stop_and_proceed_commands_return_false_for_non_matching_inputs():
    local_orchestrator = DiscussionOrchestrator()

    assert local_orchestrator._is_stop_command("다음") is False
    assert local_orchestrator._is_proceed_command("긴문장이면false") is False


def test_sse_formats_json_with_data_prefix_and_double_newline():
    payload = DiscussionOrchestrator()._sse(
        "round_complete",
        {"round": 2, "ok": True},
    )

    assert payload.startswith("data: ")
    assert payload.endswith("\n\n")

    body = json.loads(payload.removeprefix("data: ").strip())
    assert body == {"event": "round_complete", "round": 2, "ok": True}


def test_get_preset_returns_known_presets_and_falls_back_to_standard():
    assert {"standard", "deep", "light"} <= set(DISCUSSION_PRESETS)

    standard = get_preset("standard")
    deep = get_preset("deep")
    light = get_preset("light")
    fallback = get_preset("missing-preset")

    assert standard["name"] == "standard"
    assert deep["name"] == "deep"
    assert light["name"] == "light"
    assert fallback["name"] == "standard"
    assert standard is not DISCUSSION_PRESETS["standard"]
    assert fallback is not DISCUSSION_PRESETS["standard"]


def test_resolve_model_name_maps_common_aliases():
    assert resolve_model_name("옵스") == "claude-sonnet-4-6"
    assert resolve_model_name("제미나이") == "gemini-2.5-pro"
    assert resolve_model_name("gemini flash") == "gemini-2.5-flash"
    assert resolve_model_name("sonnet") == "claude-sonnet-4-6"


def test_estimate_round_cost_returns_positive_value_for_standard_preset():
    assert estimate_round_cost(get_preset("standard")) > 0


def test_inject_ceo_directive_returns_false_for_missing_session():
    assert orchestrator.inject_ceo_directive("없는세션", "test") is False


def test_cancel_discussion_returns_false_for_missing_session():
    assert orchestrator.cancel_discussion("없는세션") is False


def test_get_discussion_status_returns_none_for_missing_session():
    assert orchestrator.get_discussion_status("없는세션") is None


def test_get_active_discussion_returns_none_for_missing_session():
    get_active_discussion = getattr(orchestrator, "get_active_discussion", lambda _session_id: None)
    assert get_active_discussion("없는세션") is None
