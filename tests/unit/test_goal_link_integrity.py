"""Goal Control P0 무결성 — 명시적 바인딩 / 종료 상태 전파 / 재조정 / 승계.

DB 없이 도는 테스트다. 정규화·바인딩 파싱·재조정 계획은 순수 함수라 직접 호출하고,
GoalStateMachine / _auto_link_job_to_goal 은 최소 가짜 커넥션으로 검증한다.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services.goal_binding import (
    BIND_SOURCE_EXPLICIT_API,
    BIND_SOURCE_EXPLICIT_DIRECTIVE,
    LINK_STATE_DETACHED,
    LINK_STATE_ORPHAN,
    is_terminal_job_state,
    normalize_job_state,
    parse_goal_binding,
)
from app.services.goal_link_reconciler import plan_actions, summarize

ROOT = Path(__file__).resolve().parents[2]

GOAL_A = "1a00d8d3-1126-4a6e-8ed2-8a43f5502250"
GOAL_B = "380c796f-2524-4bfc-b9d8-a382a20096e9"
MILESTONE_A = "8519fd43-7070-49b0-8d85-1d053046cc1d"


# ─── 상태 정규화 (요구사항 A) ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "status,phase,expected",
    [
        ("done", "done", "completed"),
        ("approved", None, "completed"),
        ("deployed", None, "completed"),
        ("error", "error", "failed"),
        ("rejected_done", "rejected_done", "failed"),
        ("rejected", None, "failed"),
        ("cancelled", "blocked_dependency", "failed"),
        ("error", "review_failed", "failed"),
        # terminate_task 는 status='error' 를 쓰지만 phase 로 terminated 를 남긴다.
        ("error", "terminated", "failed"),
        ("running", "terminated", "failed"),
        # 재검수로 되살아날 수 있는 보류는 실패가 아니다 — 마일스톤을 막지 않는다.
        ("review_hold", "review_hold", "action_required"),
        ("awaiting_approval", "awaiting_approval", "action_required"),
        ("queued", "queued", "queued"),
        ("running", "running", "running"),
        (None, None, "pending"),
    ],
)
def test_terminal_status_aliases_normalize_once(status, phase, expected):
    assert normalize_job_state(status, phase) == expected


def test_review_hold_is_terminal_for_reconciliation_but_not_a_failure():
    """review_hold 는 재조정 트리거 대상이면서도 마일스톤을 막지 않아야 한다."""
    assert is_terminal_job_state("review_hold") is True
    assert normalize_job_state("review_hold") == "action_required"


def test_running_job_is_not_terminal():
    assert is_terminal_job_state("running", "claude_code_work") is False
    assert is_terminal_job_state("queued", "queued") is False


# ─── 명시적 바인딩 (요구사항 B) ─────────────────────────────────────────────
def test_unrelated_same_project_job_has_no_binding():
    """프로젝트만 같은 무관한 작업은 어떤 목표에도 붙지 않는다 (핵심 회귀)."""
    instruction = (
        "TASK_ID: runner-a16e637f\n"
        "TITLE: Complete and deploy AADS OHVIS authenticated trace ingest receiver\n"
        "PRIORITY: P0-CRITICAL\n"
    )
    binding = parse_goal_binding(instruction)
    assert binding.is_explicit is False
    assert binding.goal_id is None


def test_directive_metadata_binds_goal_and_milestone():
    instruction = f"TASK_ID: runner-1\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {MILESTONE_A}\n"
    binding = parse_goal_binding(instruction)
    assert binding.goal_id == GOAL_A
    assert binding.milestone_id == MILESTONE_A
    assert binding.source == BIND_SOURCE_EXPLICIT_DIRECTIVE


def test_explicit_api_fields_take_priority_over_directive_metadata():
    instruction = f"GOAL_ID: {GOAL_B}\n"
    binding = parse_goal_binding(instruction, goal_id=GOAL_A)
    assert binding.goal_id == GOAL_A
    assert binding.source == BIND_SOURCE_EXPLICIT_API


def test_malformed_goal_id_is_ignored_rather_than_guessed():
    assert parse_goal_binding("GOAL_ID: not-a-uuid\n").is_explicit is False
    assert parse_goal_binding(None, goal_id="12345").is_explicit is False
    # goal_id 없이 milestone_id 만 있으면 연결하지 않는다.
    assert parse_goal_binding(f"MILESTONE_ID: {MILESTONE_A}\n").is_explicit is False


def test_milestone_from_other_goal_is_dropped_not_guessed():
    binding = parse_goal_binding(None, goal_id=GOAL_A, milestone_id="bogus")
    assert binding.goal_id == GOAL_A
    assert binding.milestone_id is None


# ─── 바인딩 검증: 프로젝트/활성 상태 (요구사항 B) ───────────────────────────
class _FakeConn:
    """goals/milestones 조회만 답하는 최소 가짜 커넥션."""

    def __init__(self, goal_row=None, milestone_owned=True):
        self._goal_row = goal_row
        self._milestone_owned = milestone_owned

    async def fetchrow(self, query, *args):
        if "FROM goals" in query:
            return self._goal_row
        return None

    async def fetchval(self, query, *args):
        if "FROM milestones" in query:
            return 1 if self._milestone_owned else None
        return None

    async def fetch(self, query, *args):
        return []


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _run_auto_link(monkeypatch, goal_row, **kwargs):
    from app.services import pipeline_runner_service as prs

    linked: dict = {}
    conn = _FakeConn(goal_row, milestone_owned=kwargs.pop("milestone_owned", True))

    class _FakeMachine:
        async def _pool(self):
            return _FakePool(conn)

        async def _link_task_with_context(self, **link_kwargs):
            linked.update(link_kwargs)
            return {"link_id": "x"}

    import app.services.goal_manager as gm

    monkeypatch.setattr(gm, "goal_state_machine", _FakeMachine())
    result = asyncio.run(prs._link_job_to_goal_explicit("runner-test", "AADS", **kwargs))
    return result, linked


def test_explicit_valid_binding_creates_link(monkeypatch):
    goal_row = {"id": GOAL_A, "project": "AADS", "status": "active"}
    result, linked = _run_auto_link(monkeypatch, goal_row, goal_id=GOAL_A)
    assert result == GOAL_A
    assert linked["goal_id"] == GOAL_A
    assert linked["task_id"] == "runner-test"
    assert linked["bind_source"] == BIND_SOURCE_EXPLICIT_API


def test_cross_project_binding_is_rejected(monkeypatch):
    """GO100 목표에 AADS 작업을 붙이려 하면 연결하지 않는다."""
    goal_row = {"id": GOAL_A, "project": "GO100", "status": "active"}
    result, linked = _run_auto_link(monkeypatch, goal_row, goal_id=GOAL_A)
    assert result is None
    assert linked == {}


def test_completed_goal_binding_is_rejected(monkeypatch):
    goal_row = {"id": GOAL_A, "project": "AADS", "status": "completed"}
    result, linked = _run_auto_link(monkeypatch, goal_row, goal_id=GOAL_A)
    assert result is None
    assert linked == {}


def test_missing_goal_binding_is_rejected(monkeypatch):
    result, linked = _run_auto_link(monkeypatch, None, goal_id=GOAL_A)
    assert result is None
    assert linked == {}


def test_no_explicit_context_never_touches_the_database(monkeypatch):
    """근거가 없으면 목표 조회조차 하지 않고 조용히 건너뛴다."""
    from app.services import pipeline_runner_service as prs

    import app.services.goal_manager as gm

    class _Exploding:
        async def _pool(self):
            raise AssertionError("명시적 근거 없이 목표를 조회하면 안 된다")

    monkeypatch.setattr(gm, "goal_state_machine", _Exploding())
    assert asyncio.run(
        prs._link_job_to_goal_explicit("runner-x", "AADS", instruction="TITLE: 무관한 작업\n")
    ) is None


def test_milestone_not_owned_by_goal_falls_back_to_goal_level(monkeypatch):
    goal_row = {"id": GOAL_A, "project": "AADS", "status": "active"}
    _, linked = _run_auto_link(
        monkeypatch, goal_row, goal_id=GOAL_A, milestone_id=MILESTONE_A, milestone_owned=False,
    )
    assert linked["goal_id"] == GOAL_A
    assert linked["milestone_id"] is None


# ─── 재조정 계획 (요구사항 C) ───────────────────────────────────────────────
def _link(**overrides):
    row = {
        "link_id": "11111111-1111-1111-1111-111111111111",
        "goal_id": GOAL_A,
        "milestone_id": MILESTONE_A,
        "task_type": "pipeline_job",
        "task_id": "runner-a16e637f",
        "link_status": "queued",
        "link_state": "active",
        "bind_source": BIND_SOURCE_EXPLICIT_API,
        "superseded_by": None,
        "job_status": "error",
        "job_phase": "terminated",
        "job_project": "AADS",
        "goal_project": "AADS",
        "goal_status": "active",
    }
    row.update(overrides)
    return row


def test_stale_link_after_termination_is_planned_for_repair():
    """runner-a16e637f: terminated 인데 링크가 queued 로 남은 실제 사례."""
    actions = plan_actions([_link()])
    assert summarize(actions) == {"stale": 1}
    assert actions[0]["from_status"] == "queued"
    assert actions[0]["to_status"] == "failed"


def test_orphan_link_is_quarantined_not_deleted():
    actions = plan_actions([_link(job_status=None, job_phase=None, job_project=None)])
    assert actions[0]["kind"] == "orphan"
    assert actions[0]["new_link_state"] == LINK_STATE_ORPHAN
    assert "delete" not in str(actions[0]).lower()


def test_cross_project_link_is_detached_with_evidence():
    actions = plan_actions([_link(job_project="GO100")])
    assert actions[0]["kind"] == "misbound"
    assert actions[0]["new_link_state"] == LINK_STATE_DETACHED
    assert "cross_project" in actions[0]["reason"]


def test_legacy_auto_links_are_reported_but_not_detached_by_default():
    """프로젝트만 보고 붙었다는 사실만으로 자동 분리하지 않는다 (데이터 훼손 방지)."""
    actions = plan_actions([_link(bind_source="legacy_auto_project")])
    kinds = summarize(actions)
    assert kinds["legacy_unverified"] == 1
    assert "legacy_detach" not in kinds
    legacy = [a for a in actions if a["kind"] == "legacy_unverified"][0]
    assert legacy["report_only"] is True


def test_legacy_links_detach_only_when_operator_opts_in():
    actions = plan_actions([_link(bind_source="legacy_auto_project")], detach_legacy=True)
    assert summarize(actions) == {"legacy_detach": 1}
    assert actions[0]["new_link_state"] == LINK_STATE_DETACHED


def test_already_detached_or_orphan_links_are_left_alone():
    """멱등성: 이미 처리된 행은 두 번째 실행에서 계획에 잡히지 않는다."""
    assert plan_actions([_link(link_state="detached")]) == []
    assert plan_actions([_link(link_state="orphan")]) == []


def test_correct_link_produces_no_action():
    assert plan_actions([_link(link_status="failed")]) == []
    assert plan_actions([_link(link_status="completed", job_status="done", job_phase="done")]) == []


def test_reconciliation_plan_is_idempotent_for_the_repaired_state():
    """1회차 계획을 적용한 뒤의 행으로 다시 계획하면 아무 조치도 남지 않는다."""
    row = _link()
    first = plan_actions([row])
    assert len(first) == 1
    row["link_status"] = first[0]["to_status"]
    assert plan_actions([row]) == []


def test_one_action_per_link_no_duplicate_plans():
    rows = [_link(), _link(link_id="22222222-2222-2222-2222-222222222222")]
    actions = plan_actions(rows)
    assert len(actions) == len({a["link_id"] for a in actions}) == 2


# ─── 승계/재시도 의미론 (요구사항 D) ────────────────────────────────────────
def test_superseded_failures_are_excluded_from_milestone_evaluation():
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")

    # 승계 판정은 완료 판정보다 **먼저** 돌아야 한다.
    body = source.split("async def check_milestone_completion", 1)[1]
    assert "_mark_superseded_failures" in body.split("links = await conn.fetch", 1)[0]
    assert "superseded_link_predicate" in body

    # 승계 근거는 "같은 instruction_hash 로 나중에 성공한 작업" 하나뿐이다.
    supersede = source.split("async def _mark_superseded_failures", 1)[1].split(
        "\n    async def ", 1
    )[0]
    assert "instruction_hash = failed_job.instruction_hash" in supersede
    assert "r.created_at > failed_job.created_at" in supersede
    assert "l.status = 'failed'" in supersede
    # 승계되지 않은 실패는 그대로 남아 마일스톤을 막는다.
    assert "l.superseded_by IS NULL" in supersede


def test_unsuperseded_failure_still_blocks_the_milestone():
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")
    body = source.split("async def update_task_status_with_phase", 1)[1].split(
        "\n    async def ", 1
    )[0]
    assert "status = 'blocked'" in body
    # 승계된 경우에만 blocked 를 건너뛴다.
    assert "superseded_by IS NOT NULL" in body


def test_existing_goal_and_runner_call_signatures_are_preserved():
    """기존 호출자가 쓰는 공개/공용 함수 시그니처를 변경하지 않는다."""
    goal_source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")
    runner_source = (
        ROOT / "app" / "services" / "pipeline_runner_service.py"
    ).read_text(encoding="utf-8")
    api_source = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")

    assert "async def update_task_status(self, task_type: str, task_id: str, status: str)" in goal_source
    assert "def _normalize_task_status(self, status: str)" in goal_source
    assert 'async def _update_linked_goal_state(job_id: str, status: str = "done")' in runner_source
    assert "async def _auto_link_job_to_goal(job_id: str, project: str)" in runner_source
    assert "async def cascade_cleanup_orphans(conn, failed_job_id: str) -> int" in api_source


# ─── 종료 전파 배선 (요구사항 A/E) ─────────────────────────────────────────
def test_every_out_of_band_terminal_write_reconciles_goal_links():
    source = (ROOT / "app" / "services" / "pipeline_runner_service.py").read_text(encoding="utf-8")

    assert "async def _reconcile_job_goal_links" in source
    # 복구 스윕 / 폴링 재개 / watchdog / 강제취소 / 결과수거 경로 전부.
    assert source.count("await _reconcile_job_goal_links(") >= 8
    # 정규화는 goal_binding 단일 출처를 쓴다.
    assert "from app.services.goal_binding import is_terminal_job_state, parse_goal_binding" in source
    # 종료가 아닌 상태는 목표 그래프를 건드리지 않는다.
    assert "if not is_terminal_job_state(row[\"status\"], row[\"phase\"]):" in source


def test_terminate_task_propagates_to_goal_links():
    source = (ROOT / "app" / "services" / "tool_executor.py").read_text(encoding="utf-8")
    body = source.split("async def _terminate_task", 1)[1].split("\n    async def ", 1)[0]
    assert "_reconcile_job_goal_links" in body


def test_notify_endpoint_propagates_all_terminal_states_not_only_done():
    source = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")

    # 회귀 방지: 예전에는 done 일 때만 목표를 갱신했다.
    assert 'await _update_linked_goal_state_with_phase(job_id, status, row["phase"])' in source
    assert 'if status == "done":\n            try:\n                from app.services.pipeline_runner_service import _update_linked_goal_state' not in source
    # cascade 로 취소된 의존 작업들도 반영한다.
    assert "for orphan_job_id in orphaned_job_ids:" in source


def test_submit_api_stays_backwards_compatible_with_optional_goal_fields():
    source = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")

    # 새 필드는 선택이고 기본값이 빈 문자열이라 기존 클라이언트가 그대로 동작한다.
    assert 'goal_id: str = Field("",' in source
    assert 'milestone_id: str = Field("",' in source
    # 프로젝트만 보고 첫 active 목표를 고르던 쿼리는 사라져야 한다.
    runner = (ROOT / "app" / "services" / "pipeline_runner_service.py").read_text(encoding="utf-8")
    assert "WHERE project = $1 AND status = 'active' ORDER BY created_at LIMIT 1" not in runner


def test_no_premature_auto_advance_criteria_change():
    """다음 마일스톤/목표 개시 기준은 그대로여야 한다 (완료 판정 통과 시에만)."""
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")

    advance = source.split("async def _advance_after_milestone", 1)[1].split(
        "\n    async def ", 1
    )[0]
    assert "sequence_order > $2 AND status = 'pending'" in advance
    assert "next_ms[\"auto_advance\"]" in advance

    progress = source.split("async def _update_goal_progress", 1)[1].split("\n    async def ", 1)[0]
    assert "if total > 0 and total == completed:" in progress
    assert "WHERE project = $1 AND status = 'draft'" in progress


# ─── 마이그레이션 (요구사항 G) ──────────────────────────────────────────────
def test_migration_is_idempotent_and_never_deletes_rows():
    sql = (ROOT / "migrations" / "166_goal_link_binding_provenance.sql").read_text(encoding="utf-8")

    for column in (
        "bind_source", "link_state", "superseded_by", "superseded_at",
        "detach_reason", "last_job_status", "reconciled_at",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql

    assert "ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS goal_id UUID" in sql
    assert "ALTER TABLE pipeline_jobs ADD COLUMN IF NOT EXISTS milestone_id UUID" in sql
    assert sql.count("CREATE INDEX IF NOT EXISTS") >= 3

    # 행을 지우지 않는다 — 회수는 link_state 로만 표현한다.
    assert "DELETE FROM" not in sql.upper()
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()

    # 재실행해도 기존 bind_source 를 덮어쓰지 않는다.
    assert "WHERE bind_source IS NULL" in sql


def test_goal_manager_tolerates_pre_migration_schema():
    """migration 166 이전 이미지에서도 죽지 않아야 한다 (블루/그린 과도기)."""
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")
    assert "async def link_optional_columns" in source
    assert "information_schema.columns" in source
    assert "if \"link_state\" not in columns:" in source
