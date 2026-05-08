from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

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
    estimate_round_cost,
    get_preset,
    resolve_model_name,
)


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

    assert state.discussion_id == "disc-1"
    assert state.mode is DiscussionMode.AUTO
    assert state.status is DiscussionStatus.WAIT_CEO
    assert state.rounds[0].participant_name == "기획 A"
    assert state.rounds[0].tokens_out == 200
    assert state.participants[0].avatar == "A"


def test_build_round_context_includes_topic_history_and_directive():
    orchestrator = DiscussionOrchestrator()
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
        ],
    )

    context = orchestrator._build_round_context(
        participant,
        ceo_directive="비용 상한도 같이 검토",
        state=state,
    )

    assert "토론 주제: 멀티 LLM 토론 UX 개선" in context
    assert "현재 라운드: 2" in context
    assert "참가자: 검증 C" in context
    assert "이전 발언:" in context
    assert "기획 A (claude-opus-4-6): 초기 안은 자동 진행 중심입니다." in context
    assert "CEO 지시: 비용 상한도 같이 검토" in context


def test_is_stop_command_matches_only_stop_like_inputs():
    orchestrator = DiscussionOrchestrator()

    assert orchestrator._is_stop_command("그만")
    assert orchestrator._is_stop_command("종료")
    assert orchestrator._is_stop_command("stop")
    assert not orchestrator._is_stop_command("다음")
    assert not orchestrator._is_stop_command("진행")


def test_is_proceed_command_matches_short_proceed_inputs_only():
    orchestrator = DiscussionOrchestrator()

    assert orchestrator._is_proceed_command("다음")
    assert orchestrator._is_proceed_command("계속")
    assert orchestrator._is_proceed_command("ㄱㄱ")
    assert not orchestrator._is_proceed_command("긴문장")


def test_sse_formats_json_with_data_prefix_and_double_newline():
    orchestrator = DiscussionOrchestrator()

    sse_payload = orchestrator._sse("round_complete", {"round": 2, "ok": True})

    assert sse_payload.startswith("data: ")
    assert sse_payload.endswith("\n\n")
    body = json.loads(sse_payload[len("data: ") :].strip())
    assert body == {"event": "round_complete", "round": 2, "ok": True}


def test_get_preset_returns_known_presets_and_falls_back_to_standard():
    standard = get_preset("standard")
    deep = get_preset("deep")
    light = get_preset("light")
    fallback = get_preset("missing-preset")

    assert standard["name"] == "standard"
    assert len(standard["participants"]) == 3
    assert deep["name"] == "deep"
    assert len(deep["participants"]) == 4
    assert light["name"] == "light"
    assert len(light["participants"]) == 2
    assert fallback["name"] == "standard"


def test_resolve_model_name_maps_common_aliases():
    assert resolve_model_name("옵스") == "claude-opus-4-6"
    assert resolve_model_name("제미나이") == "gemini-2.5-pro"
    assert resolve_model_name("gemini flash") == "gemini-2.5-flash"
    assert resolve_model_name("sonnet") == "claude-sonnet-4-6"


def test_estimate_round_cost_for_standard_three_person_preset():
    standard = get_preset("standard")

    assert estimate_round_cost(standard) == 2.5


def test_session_mutation_helpers_return_falsey_without_active_session():
    orchestrator = DiscussionOrchestrator()

    assert orchestrator.inject_ceo_directive("missing", "방향 수정") is False
    assert orchestrator.cancel_discussion("missing") is False
    assert orchestrator.get_discussion_status("missing") is None


@pytest.mark.asyncio
async def test_generate_participant_reply_uses_central_llm_client_with_patch():
    orchestrator = DiscussionOrchestrator()
    participant = ParticipantConfig(
        name="기획 A",
        role="strategic_planner",
        model="claude-opus-4-6",
        system_prompt="명확하게 제안한다.",
    )

    with patch(
        "app.services.discussion_orchestrator.call_llm_with_fallback",
        new=AsyncMock(return_value="응답 텍스트"),
    ) as mock_call:
        result = await orchestrator.generate_participant_reply(
            participant,
            "토론 주제: 비용 최적화",
        )

    assert result == "응답 텍스트"
    mock_call.assert_awaited_once_with(
        prompt="토론 주제: 비용 최적화",
        model="claude-opus-4-6",
        max_tokens=1024,
        system="명확하게 제안한다.",
    )
