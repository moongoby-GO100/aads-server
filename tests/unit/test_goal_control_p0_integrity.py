"""Goal Control P0 무결성 복구 단위 테스트.

계약:
1. 명시적 목표 컨텍스트가 없는 작업은 **어떤 목표에도 자동 연결되지 않는다**.
2. 명시적 goal_id / 지시서 GOAL_ID 헤더는 그대로 연결되고 계보가 남는다.
3. 프로젝트가 다른 목표에는 연결되지 않는다 (cross-project 거부).
4. done/rejected_done/cancelled/error/terminated/review_failed/blocked_dependency 는
   한 곳(goal_binding)에서 같은 어휘로 정규화된다.
5. 재시도 성공이 있으면 과거 실패는 승계되어 마일스톤을 막지 않는다.
   승계가 없으면 실패는 그대로 보이게 남는다.
6. 재조정은 dry-run 이 기본이고, 두 번 돌려도 두 번째는 0건이다.
7. 링크 중복은 생기지 않고, 유효 링크가 없으면 마일스톤을 완료시키지 않는다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.services import goal_link_reconciler as reconciler
from app.services.goal_binding import (
    BIND_SOURCE_EXPLICIT_API,
    BIND_SOURCE_EXPLICIT_DIRECTIVE,
    LINK_STATE_ACTIVE,
    LINK_STATE_DETACHED,
    decide_supersession,
    is_terminal_job_state,
    normalize_job_state,
    parse_goal_binding,
)

ROOT = Path(__file__).resolve().parents[2]

GOAL_A = "1a00d8d3-1126-4a6e-8ed2-8a43f5502250"
GOAL_B = "a882c73e-ecd4-455e-94de-ca2528bb7331"
MILESTONE_A = "8519fd43-7070-49b0-8d85-1d053046cc1d"
T0 = datetime(2026, 9, 9, 8, 0, 0)


# ── 1. 상태 정규화 (A: 종료 전이 별칭) ──────────────────────────────────────
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
        ("terminated", None, "failed"),
        ("error", "review_failed", "failed"),
        ("error", "terminated", "failed"),
        ("running", "queued", "running"),
        ("queued", "queued", "queued"),
        ("review_hold", None, "action_required"),
        ("awaiting_approval", "awaiting_approval", "action_required"),
        (None, None, "pending"),
    ],
)
def test_terminal_status_aliases_normalize_in_one_place(status, phase, expected):
    assert normalize_job_state(status, phase) == expected


def test_terminal_detection_covers_phase_aliases():
    assert is_terminal_job_state("done")
    assert is_terminal_job_state("cancelled", "blocked_dependency")
    assert is_terminal_job_state("error", "review_failed")
    assert is_terminal_job_state("review_hold")
    assert not is_terminal_job_state("running", "claude_code_work")
    assert not is_terminal_job_state("queued", "queued")


def test_runner_terminal_set_keeps_legacy_members():
    """기존 5개 종료 상태는 하나도 빠지지 않아야 한다 (completed_at 기록 계약)."""
    from app.services.pipeline_runner_service import _TERMINAL_JOB_STATUSES

    for legacy in ("done", "error", "cancelled", "rejected_done", "review_hold"):
        assert legacy in _TERMINAL_JOB_STATUSES


# ── 2. 명시적 목표 바인딩 (B) ───────────────────────────────────────────────
def test_unrelated_job_gets_no_binding():
    """프로젝트만 같은 무관한 작업은 어떤 목표에도 붙지 않는다."""
    binding = parse_goal_binding("TASK_ID: AADS-OHVIS-TRACE-INGEST\nTITLE: trace 수신기")
    assert binding.goal_id is None
    assert not binding.is_explicit


def test_explicit_request_fields_bind():
    binding = parse_goal_binding("무관한 지시", goal_id=GOAL_A, milestone_id=MILESTONE_A)
    assert binding.goal_id == GOAL_A
    assert binding.milestone_id == MILESTONE_A
    assert binding.source == BIND_SOURCE_EXPLICIT_API


def test_directive_metadata_contract_binds():
    binding = parse_goal_binding(
        f"TASK_ID: X\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {MILESTONE_A}\nTITLE: y",
    )
    assert binding.goal_id == GOAL_A
    assert binding.milestone_id == MILESTONE_A
    assert binding.source == BIND_SOURCE_EXPLICIT_DIRECTIVE


def test_malformed_goal_id_is_not_bound():
    assert parse_goal_binding("GOAL_ID: not-a-uuid").goal_id is None
    assert parse_goal_binding("", goal_id="12345").goal_id is None


def test_bind_job_to_goal_skips_without_explicit_context(monkeypatch):
    from app.services import pipeline_runner_service as prs

    calls: list[dict] = []

    class _SM:
        async def link_task(self, **kwargs):
            calls.append(kwargs)
            return {"link_id": "x"}

    monkeypatch.setattr("app.services.goal_manager.goal_state_machine", _SM())
    result = asyncio.run(prs.bind_job_to_goal("runner-1", "AADS", instruction="아무 지시"))
    assert result == {"linked": False, "reason": "no_explicit_goal_context"}
    assert calls == []


def test_bind_job_to_goal_uses_explicit_context(monkeypatch):
    from app.services import pipeline_runner_service as prs

    calls: list[dict] = []

    class _SM:
        async def link_task(self, **kwargs):
            calls.append(kwargs)
            return {"link_id": "x", "milestone_id": kwargs["milestone_id"]}

    monkeypatch.setattr("app.services.goal_manager.goal_state_machine", _SM())
    result = asyncio.run(
        prs.bind_job_to_goal(
            "runner-1", "AADS", instruction=f"GOAL_ID: {GOAL_A}\n지시",
        )
    )
    assert result["linked"] is True
    assert calls[0]["goal_id"] == GOAL_A
    assert calls[0]["task_id"] == "runner-1"
    assert calls[0]["bind_source"] == BIND_SOURCE_EXPLICIT_DIRECTIVE


def test_bind_job_to_goal_propagates_cross_project_rejection(monkeypatch):
    from app.services import pipeline_runner_service as prs

    class _SM:
        async def link_task(self, **kwargs):
            return {"error": "project_mismatch", "goal_project": "GO100", "task_project": "AADS"}

    monkeypatch.setattr("app.services.goal_manager.goal_state_machine", _SM())
    result = asyncio.run(
        prs.bind_job_to_goal("runner-1", "AADS", goal_id=GOAL_B)
    )
    assert result["linked"] is False
    assert result["reason"] == "project_mismatch"


# ── 3. 재시도 승계 (D) ──────────────────────────────────────────────────────
def test_failed_attempt_superseded_by_later_success():
    decision = decide_supersession(
        link_status="failed",
        job_id="runner-old",
        instruction_hash="h1",
        job_created_at=T0,
        candidates=[
            {"job_id": "runner-old", "instruction_hash": "h1", "status": "error", "created_at": T0},
            {"job_id": "runner-new", "instruction_hash": "h1", "status": "done",
             "created_at": T0 + timedelta(minutes=10)},
        ],
    )
    assert decision.superseded is True
    assert decision.superseded_by == "runner-new"


def test_failed_attempt_not_superseded_when_retry_also_failed():
    decision = decide_supersession(
        link_status="failed",
        job_id="runner-old",
        instruction_hash="h1",
        job_created_at=T0,
        candidates=[
            {"job_id": "runner-new", "instruction_hash": "h1", "status": "error",
             "created_at": T0 + timedelta(minutes=10)},
        ],
    )
    assert decision.superseded is False


def test_earlier_success_does_not_supersede_current_failure():
    """현재 실패는 **이전** 성공으로 지워지지 않는다 — 계속 보이게 막혀 있어야 한다."""
    decision = decide_supersession(
        link_status="failed",
        job_id="runner-current",
        instruction_hash="h1",
        job_created_at=T0,
        candidates=[
            {"job_id": "runner-past", "instruction_hash": "h1", "status": "done",
             "created_at": T0 - timedelta(hours=1)},
        ],
    )
    assert decision.superseded is False


def test_completed_link_is_never_superseded():
    decision = decide_supersession(
        link_status="completed",
        job_id="runner-ok",
        instruction_hash="h1",
        job_created_at=T0,
        candidates=[
            {"job_id": "runner-new", "instruction_hash": "h1", "status": "done",
             "created_at": T0 + timedelta(minutes=1)},
        ],
    )
    assert decision.superseded is False


# ── 4. 재조정 계획 (C) ──────────────────────────────────────────────────────
def _link(**overrides):
    base = {
        "link_id": "11111111-1111-1111-1111-111111111111",
        "goal_id": GOAL_A,
        "milestone_id": MILESTONE_A,
        "task_id": "runner-1",
        "link_status": "queued",
        "link_state": LINK_STATE_ACTIVE,
        "bind_source": BIND_SOURCE_EXPLICIT_API,
        "superseded_by": None,
        "job_id": "runner-1",
        "job_status": "error",
        "job_phase": "error",
        "job_project": "AADS",
        "instruction_hash": "h1",
        "job_created_at": T0,
        "job_goal_id": GOAL_A,
        "instruction": "지시",
        "goal_project": "AADS",
        "goal_status": "active",
    }
    base.update(overrides)
    return base


def test_plan_marks_stale_link_status():
    actions = reconciler._plan_actions([_link()], {}, include_unverified=False)
    stale = [a for a in actions if a["action"] == reconciler.ACTION_STALE_STATUS]
    assert len(stale) == 1
    assert stale[0]["from"] == "queued"
    assert stale[0]["to"] == "failed"


def test_plan_is_idempotent_when_status_already_matches():
    actions = reconciler._plan_actions([_link(link_status="failed")], {}, include_unverified=False)
    assert [a for a in actions if a["action"] == reconciler.ACTION_STALE_STATUS] == []


def test_plan_marks_orphan_only_when_no_terminal_verdict():
    unresolved = reconciler._plan_actions(
        [_link(job_id=None, job_status=None, job_phase=None, link_status="queued")],
        {}, include_unverified=False,
    )
    assert [a["action"] for a in unresolved] == [reconciler.ACTION_ORPHAN]

    # 이미 완료 판정이 남아 있는 고아는 완료 기여분을 유지한다 (건드리지 않는다).
    resolved = reconciler._plan_actions(
        [_link(job_id=None, job_status=None, job_phase=None, link_status="completed")],
        {}, include_unverified=False,
    )
    assert resolved == []


def test_plan_detaches_cross_project_link():
    actions = reconciler._plan_actions(
        [_link(job_project="GO100", goal_project="AADS")], {}, include_unverified=False,
    )
    assert [a["action"] for a in actions] == [reconciler.ACTION_MISBOUND_PROJECT]


def test_plan_reports_unverified_binding_without_repairing_by_default():
    link = _link(bind_source="unknown", job_goal_id=None, instruction="GOAL_ID 없음")
    reported = reconciler._plan_actions([link], {}, include_unverified=False)
    unverified = [a for a in reported if a["action"] == reconciler.ACTION_UNVERIFIED_BIND]
    assert len(unverified) == 1
    assert unverified[0]["applied"] is False
    # 회수하지 않아도 상태 정합은 계속 맞춘다.
    assert any(a["action"] == reconciler.ACTION_STALE_STATUS for a in reported)

    opted_in = reconciler._plan_actions([link], {}, include_unverified=True)
    assert [a["action"] for a in opted_in] == [reconciler.ACTION_UNVERIFIED_BIND]
    assert opted_in[0]["applied"] is True


def test_plan_keeps_unverified_link_when_job_declares_the_goal():
    """작업 스스로 이 목표를 명시했다면 계보가 unknown 이어도 오연결이 아니다."""
    link = _link(bind_source="unknown", job_goal_id=None,
                 instruction=f"GOAL_ID: {GOAL_A}\n지시", link_status="failed")
    actions = reconciler._plan_actions([link], {}, include_unverified=True)
    assert [a for a in actions if a["action"] == reconciler.ACTION_UNVERIFIED_BIND] == []


def test_plan_skips_already_reconciled_links():
    """이미 회수/승계된 링크는 다시 계획에 오르지 않는다 (멱등)."""
    detached = _link(link_state=LINK_STATE_DETACHED)
    assert reconciler._plan_actions([detached], {}, include_unverified=True) == []


def test_plan_supersedes_failed_link_with_successful_retry():
    link = _link(link_status="failed", job_status="error", job_phase="error")
    candidates = {
        "h1": [
            {"job_id": "runner-2", "instruction_hash": "h1", "status": "done",
             "phase": "done", "created_at": T0 + timedelta(minutes=5)},
        ]
    }
    actions = reconciler._plan_actions([link], candidates, include_unverified=False)
    supersede = [a for a in actions if a["action"] == reconciler.ACTION_SUPERSEDE]
    assert len(supersede) == 1
    assert supersede[0]["superseded_by"] == "runner-2"


def test_plan_leaves_unsuperseded_failure_visible():
    link = _link(link_status="failed", job_status="error", job_phase="error")
    actions = reconciler._plan_actions([link], {"h1": []}, include_unverified=False)
    assert [a for a in actions if a["action"] == reconciler.ACTION_SUPERSEDE] == []


# ── 5. sync_job_status (A: durable write 이후 1회/멱등 전파) ─────────────────
class _FakeConn:
    def __init__(self, job_row=None, has_link=True):
        self.job_row = job_row
        self.has_link = has_link

    async def fetchrow(self, query, *args):
        return self.job_row

    async def fetchval(self, query, *args):
        return self.has_link


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


def test_sync_skips_when_job_has_no_goal_link(monkeypatch):
    conn = _FakeConn(job_row={"status": "error", "phase": "error"}, has_link=False)

    async def _pool():
        return _FakePool(conn)

    monkeypatch.setattr(reconciler, "_pool", _pool)
    updates: list = []

    class _SM:
        async def update_task_status(self, *args, **kwargs):
            updates.append(args)
            return {"updated": 1}

    monkeypatch.setattr("app.services.goal_manager.goal_state_machine", _SM())
    result = asyncio.run(reconciler.sync_job_status("runner-1", "error", "error"))
    assert result["synced"] is False
    assert result["reason"] == "no_linked_goal"
    assert updates == []


def test_sync_propagates_terminal_status(monkeypatch):
    conn = _FakeConn(job_row={"status": "error", "phase": "terminated"}, has_link=True)

    async def _pool():
        return _FakePool(conn)

    monkeypatch.setattr(reconciler, "_pool", _pool)
    updates: list = []

    class _SM:
        async def update_task_status(self, *args, **kwargs):
            updates.append(args)
            return {"updated": 1}

    monkeypatch.setattr("app.services.goal_manager.goal_state_machine", _SM())
    result = asyncio.run(reconciler.sync_job_status("runner-1"))
    assert result["synced"] is True
    assert updates == [("pipeline_job", "runner-1", "error", "terminated")]


def test_sync_if_terminal_ignores_in_flight_status(monkeypatch):
    def _boom():
        raise AssertionError("진행 중 상태에서는 DB 를 건드리면 안 된다")

    monkeypatch.setattr(reconciler, "_pool", _boom)
    result = asyncio.run(
        reconciler.sync_job_status_if_terminal("runner-1", "running", "claude_code_work")
    )
    assert result["reason"] == "not_terminal"


def test_sync_never_raises_on_db_failure(monkeypatch):
    async def _pool():
        raise RuntimeError("pool down")

    monkeypatch.setattr(reconciler, "_pool", _pool)
    result = asyncio.run(reconciler.sync_job_status("runner-1", "done"))
    assert result["synced"] is False
    assert result["reason"] == "error"


# ── 6. 마이그레이션/배선 정적 검증 (G) ──────────────────────────────────────
def test_migration_is_additive_and_idempotent():
    sql = (ROOT / "migrations" / "165_goal_link_binding_and_supersession.sql").read_text(
        encoding="utf-8"
    )
    for column in ("link_state", "bind_source", "superseded_by", "superseded_at",
                   "supersede_reason", "reconciled_at", "reconcile_note"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, column
    assert "ADD COLUMN IF NOT EXISTS goal_id UUID" in sql
    assert "ADD COLUMN IF NOT EXISTS milestone_id UUID" in sql
    # 파괴적 SQL 금지 (deploy_release_assets.sh 가 DROP/TRUNCATE 를 차단한다)
    assert "DROP " not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()


def test_terminal_write_sites_reconcile_goal_links():
    """durable 종료 write 마다 목표 반영이 배선돼 있어야 한다."""
    runner = (ROOT / "app" / "services" / "pipeline_runner_service.py").read_text(encoding="utf-8")
    api = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")
    executor = (ROOT / "app" / "services" / "tool_executor.py").read_text(encoding="utf-8")
    qa = (ROOT / "app" / "api" / "qa.py").read_text(encoding="utf-8")
    chat_tools = (ROOT / "app" / "api" / "ceo_chat_tools.py").read_text(encoding="utf-8")
    cleanup = (ROOT / "app" / "services" / "pipeline_cleanup.py").read_text(encoding="utf-8")

    # 러너: 저장/취소/폴링완료/타임아웃/watchdog/고아수거 6개 경로
    assert runner.count("_update_linked_goal_state(") >= 7
    # API: 알림(모든 종료 상태), 승인/거부, 고아 캐스케이드, 의존 고아 정리
    assert "if is_terminal_job_state(status, row[\"phase\"]):" in api
    assert "source=\"approve_or_reject\"" in api
    assert "source=\"cascade_cleanup_orphans\"" in api
    # terminate_task (도구/CEO 채팅 양쪽)
    assert "source=\"terminate_task\"" in executor
    assert "source=\"ceo_terminate_task\"" in chat_tools
    assert "sync_job_status_if_terminal" in qa
    assert "mark_superseded" in cleanup


def test_submit_api_binds_only_with_explicit_context():
    api = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")
    assert "goal_id: str = Field(\"\"" in api
    assert "if goal_binding.is_explicit:" in api
    # 프로젝트만 보고 붙이던 옛 자동연결은 호출되지 않아야 한다.
    assert "_auto_link_job_to_goal" not in api


def test_goal_manager_uses_effective_links_only():
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")
    assert "_EFFECTIVE_LINK_SQL" in source
    assert "link_state = 'active' AND superseded_by IS NULL" in source
    assert "no_effective_tasks" in source
    assert "project_mismatch" in source
