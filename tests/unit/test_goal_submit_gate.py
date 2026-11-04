"""러너 제출 GOAL_ID·MILESTONE_ID 짝 게이트 (goal.milestone_id_missing_on_submit).

DB 는 쓰지 않는다 — 마일스톤 조회 경계만 대역으로 바꾼다.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import goal_submit_gate as gate
from app.services.goal_binding import (
    BIND_SOURCE_EXPLICIT_API,
    BIND_SOURCE_EXPLICIT_DIRECTIVE,
    EMPTY_BINDING,
    parse_goal_binding,
    resolve_declared_ids,
)

TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION = "22222222-2222-4222-8222-222222222222"
GOAL_A = "11111111-1111-4111-8111-111111111111"
GOAL_B = "11111111-1111-4111-8111-222222222222"
MS_A = "55555555-5555-4555-8555-555555555551"
MS_B = "55555555-5555-4555-8555-555555555552"


@pytest.fixture
def milestones(monkeypatch):
    table = {MS_A: GOAL_A, MS_B: GOAL_B}

    async def fetch(milestone_id, tenant_id):
        goal = table.get(milestone_id)
        return {"milestone_id": milestone_id, "goal_id": goal} if goal else None

    monkeypatch.setattr(gate, "_fetch_milestone", fetch)
    return table


@pytest.fixture
def owned(monkeypatch):
    rows: list[dict] = []

    async def fetch(tenant_id, session_id, project):
        return rows

    monkeypatch.setattr(gate, "_owned_open_milestones", fetch)
    monkeypatch.delenv(gate.LINK_MISSING_MODE_ENV, raising=False)
    return rows


def _eval(instruction="", goal_id=None, milestone_id=None):
    return asyncio.run(gate.evaluate_submit_goal_pair(
        instruction=instruction, goal_id=goal_id, milestone_id=milestone_id,
        tenant_id=TENANT, session_id=SESSION, project="NTV2",
    ))


def test_goal_without_milestone_is_rejected(milestones, owned):
    verdict = _eval(f"TASK\nGOAL_ID: {GOAL_A}\n")
    assert not verdict.allowed
    assert verdict.code == "goal.milestone_id_missing_on_submit"
    assert verdict.message == "MILESTONE_ID 가 없습니다. my_milestones 로 id 를 확인해 지시서에 넣으십시오"


def test_goal_argument_without_milestone_is_rejected(milestones, owned):
    assert not _eval("작업", goal_id=GOAL_A).allowed


def test_milestone_only_fills_goal_from_milestone(milestones, owned):
    verdict = _eval(f"TASK\nMILESTONE_ID: {MS_A}\n")
    assert verdict.allowed
    assert (verdict.goal_id, verdict.milestone_id) == (GOAL_A, MS_A)


def test_milestone_of_other_goal_is_rejected(milestones, owned):
    verdict = _eval(f"GOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_B}\n")
    assert not verdict.allowed
    assert verdict.code == "goal.milestone_goal_mismatch_on_submit"
    assert GOAL_A in verdict.message and GOAL_B in verdict.message


def test_unknown_milestone_is_rejected(milestones, owned):
    verdict = _eval(f"GOAL_ID: {GOAL_A}\nMILESTONE_ID: 55555555-5555-4555-8555-5555555555ff\n")
    assert not verdict.allowed and verdict.code == "goal.milestone_not_found_on_submit"


def test_valid_pair_passes_with_both_ids(milestones, owned):
    verdict = _eval(f"GOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n")
    assert verdict.allowed and (verdict.goal_id, verdict.milestone_id) == (GOAL_A, MS_A)
    assert verdict.warning is None


def test_pair_may_come_from_submit_arguments(milestones, owned):
    verdict = _eval("작업", goal_id=GOAL_A, milestone_id=MS_A)
    assert verdict.allowed and verdict.milestone_id == MS_A


def test_submission_without_goal_context_still_passes(milestones, owned):
    verdict = _eval("목표와 무관한 지시")
    assert verdict.allowed and verdict.goal_id is None and verdict.warning is None


def test_owner_session_without_goal_id_warns_by_default(milestones, owned, caplog):
    owned.append({"milestone_id": MS_A, "goal_id": GOAL_A})
    with caplog.at_level("WARNING"):
        verdict = _eval("목표 표기 없는 지시")
    assert verdict.allowed
    assert verdict.warning and "GOAL_ID" in verdict.warning
    assert any("goal_link_missing" in r.getMessage() for r in caplog.records)


def test_owner_session_without_goal_id_blocks_when_switch_is_block(milestones, owned, monkeypatch):
    owned.append({"milestone_id": MS_A, "goal_id": GOAL_A})
    monkeypatch.setenv(gate.LINK_MISSING_MODE_ENV, "block")
    verdict = _eval("목표 표기 없는 지시")
    assert not verdict.allowed and verdict.code == "goal.link_missing_on_submit"


def _raise_db(monkeypatch, *names):
    async def boom(*_a, **_k):
        raise RuntimeError("db down")

    for name in names:
        monkeypatch.setattr(gate, name, boom)


def test_explicit_id_lookup_failure_is_retryable_rejection_not_a_pass(monkeypatch):
    """명시 ID 쌍은 조회 예외가 나도 통과시키지 않는다 — 잘못된 쌍도 올바른 쌍도 503."""
    _raise_db(monkeypatch, "_fetch_milestone")
    for instruction in (
        f"GOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n",
        f"GOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_B}\n",
        f"MILESTONE_ID: {MS_A}\n",
    ):
        verdict = _eval(instruction)
        assert not verdict.allowed
        assert (verdict.status_code, verdict.code) == (503, "goal.milestone_lookup_failed_on_submit")
        assert "다시 제출" in verdict.message


def test_lookup_failure_recovers_on_resubmit(milestones, owned, monkeypatch):
    real = gate._fetch_milestone
    _raise_db(monkeypatch, "_fetch_milestone")
    instruction = f"GOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n"
    assert not _eval(instruction).allowed
    monkeypatch.setattr(gate, "_fetch_milestone", real)
    verdict = _eval(instruction)
    assert verdict.allowed and (verdict.goal_id, verdict.milestone_id) == (GOAL_A, MS_A)


def test_warning_lookup_failure_for_goalless_submission_stays_fail_open_in_warn_mode(monkeypatch):
    """목표 미지정 + 담당 조회 실패: 기본(warn)에서는 경고용 보조 조회라 제출을 막지 않는다."""
    _raise_db(monkeypatch, "_owned_open_milestones")
    monkeypatch.delenv(gate.LINK_MISSING_MODE_ENV, raising=False)
    verdict = _eval("목표 표기 없는 지시")
    assert verdict.allowed and verdict.goal_id is None and verdict.warning is None


def test_warning_lookup_failure_blocks_retryably_in_block_mode(monkeypatch):
    _raise_db(monkeypatch, "_owned_open_milestones")
    monkeypatch.setenv(gate.LINK_MISSING_MODE_ENV, "block")
    verdict = _eval("목표 표기 없는 지시")
    assert not verdict.allowed and verdict.status_code == 503


def test_resolver_reads_each_field_independently_and_keeps_sources():
    assert resolve_declared_ids(f"MILESTONE_ID: {MS_A}\n") == (
        None, MS_A, None, BIND_SOURCE_EXPLICIT_DIRECTIVE, None, None)
    assert resolve_declared_ids(f"GOAL_ID: {GOAL_A}\n").goal_id == GOAL_A
    assert resolve_declared_ids("GOAL_ID: not-a-uuid\n")[:2] == (None, None)
    mixed = resolve_declared_ids(f"GOAL_ID: {GOAL_A}\n", milestone_id=MS_A)
    assert (mixed.goal_id, mixed.milestone_id) == (GOAL_A, MS_A)
    assert (mixed.goal_source, mixed.milestone_source) == (
        BIND_SOURCE_EXPLICIT_DIRECTIVE, BIND_SOURCE_EXPLICIT_API)
    same = resolve_declared_ids(f"GOAL_ID: {GOAL_A.upper()}\n", goal_id=GOAL_A)
    assert same.goal_id == GOAL_A and same.conflict is None and same.goal_source == BIND_SOURCE_EXPLICIT_API


def test_resolver_marks_argument_directive_conflict_without_choosing():
    goal = resolve_declared_ids(f"GOAL_ID: {GOAL_B}\nMILESTONE_ID: {MS_A}\n", goal_id=GOAL_A)
    assert goal.conflict == "goal" and goal.conflict_values == (GOAL_A, GOAL_B)
    assert (goal.goal_id, goal.milestone_id) == (None, None)
    milestone = resolve_declared_ids(f"MILESTONE_ID: {MS_B}\n", goal_id=GOAL_A, milestone_id=MS_A)
    assert milestone.conflict == "milestone" and milestone.milestone_id is None


def test_binding_reads_exactly_what_resolver_reads():
    """연결기(parse_goal_binding)와 게이트(resolve_declared_ids)는 같은 쌍을 읽는다 — 충돌은 연결 안 함."""
    cases = [
        (f"MILESTONE_ID: {MS_A}\n", GOAL_A, None),
        (f"GOAL_ID: {GOAL_A}\n", None, MS_A),
        (f"GOAL_ID: {GOAL_B}\nMILESTONE_ID: {MS_B}\n", GOAL_A, MS_A),
        (f"GOAL_ID: {GOAL_B}\nMILESTONE_ID: {MS_A}\n", GOAL_A, None),
        ("작업", GOAL_A, MS_A),
        (f"GOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n", None, None),
    ]
    for instruction, goal, milestone in cases:
        declared = resolve_declared_ids(instruction, goal_id=goal, milestone_id=milestone)
        binding = parse_goal_binding(instruction, goal_id=goal, milestone_id=milestone)
        if declared.conflict or not declared.goal_id:
            assert binding == EMPTY_BINDING
        else:
            assert (binding.goal_id, binding.milestone_id) == (declared.goal_id, declared.milestone_id)


def test_conflicting_argument_and_directive_goal_is_rejected_with_korean_message(milestones, owned):
    verdict = _eval(f"작업\nGOAL_ID: {GOAL_B}\nMILESTONE_ID: {MS_A}\n", goal_id=GOAL_A)
    assert not verdict.allowed
    assert (verdict.status_code, verdict.code) == (422, "goal.id_conflict_on_submit")
    assert GOAL_A in verdict.message and GOAL_B in verdict.message and "서로 다릅니다" in verdict.message


def test_conflicting_argument_and_directive_milestone_is_rejected(milestones, owned):
    verdict = _eval(f"작업\nMILESTONE_ID: {MS_B}\n", goal_id=GOAL_A, milestone_id=MS_A)
    assert not verdict.allowed and verdict.code == "goal.id_conflict_on_submit"
    assert "milestone_id" in verdict.message


def test_allowed_verdict_carries_verified_pair_with_source_kept_separately(milestones, owned):
    """검증한 쌍은 그대로 연결에 넘기고, 출처(인자/지시서/보충)는 별도 값이다."""
    api_ms_directive_goal = _eval(f"작업\nGOAL_ID: {GOAL_A}\n", milestone_id=MS_A)
    assert (api_ms_directive_goal.goal_id, api_ms_directive_goal.milestone_id) == (GOAL_A, MS_A)
    assert (api_ms_directive_goal.goal_source, api_ms_directive_goal.milestone_source) == (
        BIND_SOURCE_EXPLICIT_DIRECTIVE, BIND_SOURCE_EXPLICIT_API)
    assert api_ms_directive_goal.bind_source == BIND_SOURCE_EXPLICIT_DIRECTIVE
    assert not api_ms_directive_goal.goal_supplemented

    supplemented = _eval("작업", milestone_id=MS_A)
    assert supplemented.goal_supplemented and supplemented.goal_source is None
    assert supplemented.bind_source == BIND_SOURCE_EXPLICIT_API

    api_pair = _eval("작업", goal_id=GOAL_A, milestone_id=MS_A)
    assert api_pair.bind_source == BIND_SOURCE_EXPLICIT_API


def test_submission_without_goal_context_has_no_bind_source(milestones, owned):
    verdict = _eval("목표와 무관한 지시")
    assert verdict.bind_source is None and verdict.milestone_id is None
