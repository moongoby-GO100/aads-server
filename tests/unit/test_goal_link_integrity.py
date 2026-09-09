"""Goal Control P0 — 명시적 바인딩 / 종결 정합화 / 재시도 supersession 테스트.

DB 없이 돌린다. 바인딩 판정과 정합화 분류는 순수 로직이라 스텁 커넥션으로 검증하고,
호출부 배선은 소스 정적 검사로 확인한다 (프로덕션 DB 쓰기 금지).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.services.goal_binding import (
    BIND_SOURCE_DIRECTIVE,
    BIND_SOURCE_REQUEST,
    parse_goal_directives,
    resolve_goal_binding,
)
from app.services.goal_link_reconciler import (
    MISBOUND,
    ORPHAN,
    STALE,
    SUPERSEDABLE,
    UNVERIFIED,
    classify_links,
)
from app.services.goal_manager import is_terminal_task_status, normalize_task_status

ROOT = Path(__file__).resolve().parents[2]

GOAL_A = "1a00d8d3-1126-4a6e-8ed2-8a43f5502250"
GOAL_B = "380c796f-2524-4bfc-b9d8-a382a20096e9"
MS_A = "8519fd43-7070-49b0-8d85-1d053046cc1d"
MS_B = "9629fd43-7070-49b0-8d85-1d053046cc1e"


class StubConn:
    """goals/milestones 조회만 흉내내는 최소 커넥션."""

    def __init__(self, goals=None, milestones=None):
        self._goals = goals or {}
        self._milestones = milestones or {}

    async def fetchrow(self, sql, *args):
        key = str(args[0]).lower() if args else None
        if "FROM goals" in sql:
            return self._goals.get(key)
        if "FROM milestones" in sql:
            return self._milestones.get(key)
        return None


def _conn():
    return StubConn(
        goals={
            GOAL_A: {"id": GOAL_A, "project": "AADS", "status": "active"},
            GOAL_B: {"id": GOAL_B, "project": "NTV2", "status": "active"},
        },
        milestones={
            MS_A: {"id": MS_A, "goal_id": GOAL_A},
            MS_B: {"id": MS_B, "goal_id": GOAL_B},
        },
    )


# ─── B: 명시적 바인딩 ────────────────────────────────────────────────────────

async def test_unrelated_same_project_job_is_not_auto_linked():
    """같은 프로젝트라는 이유만으로 활성 목표에 붙지 않는다 (P0 결함 #1)."""
    decision = await resolve_goal_binding(
        _conn(), "AADS",
        instruction="OHVIS trace 수신기를 추가한다. 목표 언급 없음.",
    )
    assert decision["bound"] is False
    assert decision["reason"] == "no_explicit_goal_context"
    assert decision["goal_id"] is None


async def test_explicit_request_binding_is_accepted():
    decision = await resolve_goal_binding(
        _conn(), "AADS", goal_id=GOAL_A, milestone_id=MS_A,
    )
    assert decision["bound"] is True
    assert decision["goal_id"] == GOAL_A
    assert decision["milestone_id"] == MS_A
    assert decision["bind_source"] == BIND_SOURCE_REQUEST


async def test_directive_metadata_binding_is_accepted():
    decision = await resolve_goal_binding(
        _conn(), "AADS",
        instruction=f"TITLE: 작업\nGOAL_ID: {GOAL_A}\nMILESTONE_ID: {MS_A}\n본문...",
    )
    assert decision["bound"] is True
    assert decision["bind_source"] == BIND_SOURCE_DIRECTIVE
    assert decision["milestone_id"] == MS_A


def test_parse_goal_directives_handles_absent_and_listed_forms():
    assert parse_goal_directives(None) == (None, None)
    assert parse_goal_directives("본문에 아무 것도 없음") == (None, None)
    goal, ms = parse_goal_directives(f"- GOAL_ID: {GOAL_A}\n- MILESTONE_ID: {MS_A}")
    assert (goal, ms) == (GOAL_A, MS_A)
    # 형식이 어긋난 값은 무시한다 (uuid 아님)
    assert parse_goal_directives("GOAL_ID: not-a-uuid") == (None, None)


async def test_cross_project_binding_is_rejected():
    """다른 프로젝트의 목표에는 붙일 수 없다."""
    decision = await resolve_goal_binding(_conn(), "AADS", goal_id=GOAL_B)
    assert decision["bound"] is False
    assert decision["reason"] == "goal_project_mismatch"


async def test_milestone_belonging_to_other_goal_is_rejected():
    decision = await resolve_goal_binding(
        _conn(), "AADS", goal_id=GOAL_A, milestone_id=MS_B,
    )
    assert decision["bound"] is False
    assert decision["reason"] == "milestone_goal_mismatch"


async def test_unknown_and_closed_goals_are_rejected():
    conn = StubConn(goals={
        GOAL_A: {"id": GOAL_A, "project": "AADS", "status": "completed"},
    })
    closed = await resolve_goal_binding(conn, "AADS", goal_id=GOAL_A)
    assert closed["bound"] is False and closed["reason"] == "goal_not_open"

    missing = await resolve_goal_binding(StubConn(), "AADS", goal_id=GOAL_A)
    assert missing["bound"] is False and missing["reason"] == "goal_not_found"


# ─── A: 종결 상태 정규화 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [
    "done", "approved", "deployed", "completed", "SUCCESS", "succeeded",
])
def test_completed_aliases_normalize_to_completed(status):
    assert normalize_task_status(status) == "completed"
    assert is_terminal_task_status(status) is True


@pytest.mark.parametrize("status", [
    "error", "cancelled", "canceled", "rejected", "rejected_done",
    "terminated", "killed", "review_failed", "blocked_dependency",
    "timeout", "failed", "aborted",
])
def test_failure_aliases_normalize_to_failed(status):
    """terminate/cancel/의존성 고아 경로의 상태가 전부 종결로 인식돼야 한다."""
    assert normalize_task_status(status) == "failed"
    assert is_terminal_task_status(status) is True


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_approval", "", None])
def test_non_terminal_statuses_are_not_terminal(status):
    assert is_terminal_task_status(status) is False


# ─── C/D: 정합화 분류 ────────────────────────────────────────────────────────

def _link(**over):
    base = {
        "link_id": "11111111-1111-1111-1111-111111111111",
        "goal_id": GOAL_A,
        "milestone_id": MS_A,
        "task_type": "pipeline_job",
        "task_id": "runner-aaaaaaaa",
        "link_status": "queued",
        "goal_project": "AADS",
        "goal_status": "active",
        "job_id": "runner-aaaaaaaa",
        "job_status": "rejected_done",
        "job_phase": "rejected_done",
        "job_project": "AADS",
        "instruction_hash": "hash-1",
        "instruction_md5": "md5-1",
        "job_created_at": datetime(2026, 9, 9, 8, 0),
        "bind_source": "legacy_auto",
        "superseded_by": None,
    }
    base.update(over)
    return base


def test_terminated_job_with_queued_link_is_reported_stale():
    """runner-a16e637f 재현: 작업은 종결, 링크는 queued."""
    findings = classify_links([_link(task_id="runner-a16e637f", job_id="runner-a16e637f")])
    assert len(findings[STALE]) == 1
    assert findings[STALE][0]["target_status"] == "failed"


def test_link_already_matching_job_status_is_not_stale():
    findings = classify_links([_link(link_status="failed")])
    assert findings[STALE] == []


def test_orphan_link_without_job_row_is_reported():
    findings = classify_links([_link(job_id=None, job_status=None, job_project=None)])
    assert len(findings[ORPHAN]) == 1
    assert findings[ORPHAN][0]["task_id"] == "runner-aaaaaaaa"


def test_cross_project_link_is_reported_misbound():
    findings = classify_links([_link(job_project="NTV2")])
    assert len(findings[MISBOUND]) == 1
    assert findings[STALE] == []


def test_failed_attempt_superseded_by_later_successful_retry():
    """재시도가 성공했으면 과거 실패는 마일스톤을 막지 않는다 (D)."""
    failed = _link(task_id="runner-old", job_id="runner-old", job_status="error",
                   job_created_at=datetime(2026, 9, 9, 8, 0))
    retry = _link(link_id="22222222-2222-2222-2222-222222222222",
                  task_id="runner-new", job_id="runner-new", job_status="done",
                  link_status="completed",
                  job_created_at=datetime(2026, 9, 9, 9, 0))
    findings = classify_links([failed, retry])
    assert len(findings[SUPERSEDABLE]) == 1
    assert findings[SUPERSEDABLE][0]["superseded_by"] == "runner-new"
    assert findings[SUPERSEDABLE][0]["task_id"] == "runner-old"


def test_current_failure_without_replacement_stays_blocking():
    """대체 재시도가 없으면 실패는 supersede 되지 않고 그대로 드러나야 한다."""
    findings = classify_links([_link(task_id="runner-old", job_id="runner-old",
                                     job_status="error")])
    assert findings[SUPERSEDABLE] == []
    assert len(findings[STALE]) == 1  # queued → failed 로 정정 대상


def test_retry_with_different_instruction_does_not_supersede():
    """다른 작업이 우연히 성공했다고 실패가 지워지면 안 된다."""
    failed = _link(task_id="runner-old", job_id="runner-old", job_status="error",
                   instruction_hash="hash-1", instruction_md5="md5-1",
                   job_created_at=datetime(2026, 9, 9, 8, 0))
    unrelated = _link(link_id="33333333-3333-3333-3333-333333333333",
                      task_id="runner-other", job_id="runner-other", job_status="done",
                      link_status="completed",
                      instruction_hash="hash-2", instruction_md5="md5-2",
                      job_created_at=datetime(2026, 9, 9, 9, 0))
    findings = classify_links([failed, unrelated])
    assert findings[SUPERSEDABLE] == []


def test_earlier_success_does_not_supersede_later_failure():
    """시간 순서가 반대면 supersede 아니다 — 현행 실패가 최신이다."""
    success = _link(task_id="runner-first", job_id="runner-first", job_status="done",
                    link_status="completed", job_created_at=datetime(2026, 9, 9, 7, 0))
    failed = _link(link_id="44444444-4444-4444-4444-444444444444",
                   task_id="runner-second", job_id="runner-second", job_status="error",
                   job_created_at=datetime(2026, 9, 9, 9, 0))
    findings = classify_links([success, failed])
    assert findings[SUPERSEDABLE] == []


def test_already_superseded_link_is_skipped_for_idempotency():
    """두 번째 정합화는 변경할 것이 없어야 한다 (멱등)."""
    failed = _link(task_id="runner-old", job_id="runner-old", job_status="error",
                   superseded_by="runner-new", job_created_at=datetime(2026, 9, 9, 8, 0))
    retry = _link(link_id="55555555-5555-5555-5555-555555555555",
                  task_id="runner-new", job_id="runner-new", job_status="done",
                  link_status="completed", job_created_at=datetime(2026, 9, 9, 9, 0))
    findings = classify_links([failed, retry])
    assert findings[SUPERSEDABLE] == []
    assert findings[STALE] == []


def test_legacy_binding_is_reported_but_never_auto_repaired():
    """근거 없는 과거 바인딩은 보고만 한다 — 자동 분리/삭제 금지."""
    findings = classify_links([_link(job_status="running", link_status="running",
                                     bind_source="legacy_auto")])
    assert len(findings[UNVERIFIED]) == 1
    assert findings[MISBOUND] == [] and findings[STALE] == []


def test_explicitly_bound_healthy_link_produces_no_findings():
    findings = classify_links([_link(job_status="running", link_status="running",
                                     bind_source="request")])
    assert all(items == [] for items in findings.values())


def test_each_link_lands_in_exactly_one_category():
    rows = [
        _link(task_id="a", job_id="a", job_project="NTV2"),
        _link(task_id="b", job_id=None, job_status=None, job_project=None),
        _link(task_id="c", job_id="c", job_status="error"),
    ]
    findings = classify_links(rows)
    total = sum(len(items) for items in findings.values())
    assert total == len(rows)


# ─── A/E: 호출부 배선 정적 검증 ──────────────────────────────────────────────

def test_every_direct_terminal_write_reconciles_goal_links():
    """_save_to_db 를 우회해 pipeline_jobs 를 직접 종결시키는 경로 전부 배선 확인."""
    runner_svc = (ROOT / "app" / "services" / "pipeline_runner_service.py").read_text(encoding="utf-8")
    assert "async def _reconcile_job_goal_links(job_id: str)" in runner_svc
    assert "def schedule_goal_link_reconcile(job_id: str)" in runner_svc
    # 취소 / 폴링 재개 완료 / 폴링 타임아웃 / watchdog 자동종료
    assert runner_svc.count("_reconcile_job_goal_links(job_id)") >= 3
    assert "_reconcile_job_goal_links(job.job_id)" in runner_svc

    tool_exec = (ROOT / "app" / "services" / "tool_executor.py").read_text(encoding="utf-8")
    assert "schedule_goal_link_reconcile(task_id)" in tool_exec

    ceo_tools = (ROOT / "app" / "api" / "ceo_chat_tools.py").read_text(encoding="utf-8")
    assert "schedule_goal_link_reconcile(task_id)" in ceo_tools

    qa = (ROOT / "app" / "api" / "qa.py").read_text(encoding="utf-8")
    assert "_reconcile_job_goal_links(job_id)" in qa

    api = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")
    assert "def _schedule_goal_reconcile(job_id: str)" in api
    assert api.count("_schedule_goal_reconcile(") >= 3  # 정의 + 고아 정리 2곳


def test_approval_path_propagates_all_terminal_statuses_not_only_done():
    api = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")
    assert "_update_linked_goal_state(job_id, status)" in api
    assert '_update_linked_goal_state(job_id, "done")' not in api


def test_submit_api_exposes_optional_goal_binding_fields():
    api = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")
    assert "goal_id: str = Field(\"\"" in api
    assert "milestone_id: str = Field(\"\"" in api
    # 기존 필드는 그대로 유지 (하위 호환)
    for field in ("project:", "instruction:", "session_id:", "max_cycles:", "depends_on:"):
        assert field in api


def test_auto_link_requires_explicit_context():
    runner_svc = (ROOT / "app" / "services" / "pipeline_runner_service.py").read_text(encoding="utf-8")
    body = runner_svc.split("async def _auto_link_job_to_goal", 1)[1].split("\n# ", 1)[0]
    # 구 project-only 조회는 사라져야 한다
    assert "status = 'active' ORDER BY created_at LIMIT 1" not in body
    assert "resolve_goal_binding" in body
    assert 'if not decision["bound"]' in body


def test_goals_api_exposes_reconcile_endpoints_and_keeps_existing_ones():
    source = (ROOT / "app" / "routers" / "goals.py").read_text(encoding="utf-8")
    assert '@router.get("/goals/links/audit")' in source
    assert '@router.post("/goals/links/reconcile")' in source
    # 기존 엔드포인트 보존
    for route in (
        '@router.post("/goals/{goal_id}/advance")',
        '@router.post("/goals/advance")',
        '@router.post("/goals/task-status")',
        '@router.post("/goals/{goal_id}/link-task")',
    ):
        assert route in source


def test_reconciler_never_deletes_rows():
    """수리는 상태/분리만 한다 — DELETE 금지."""
    source = (ROOT / "app" / "services" / "goal_link_reconciler.py").read_text(encoding="utf-8")
    assert "DELETE FROM" not in source.upper()
    assert "TRUNCATE TABLE" not in source.upper()
    assert "dry_run: bool = True" in source


def test_migration_is_idempotent_and_non_destructive():
    sql = (ROOT / "migrations" / "165_goal_link_binding_provenance.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS bind_source" in sql
    assert "ADD COLUMN IF NOT EXISTS superseded_by" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql
    assert "DROP " not in sql.upper()
    assert "DELETE " not in sql.upper()


def test_milestone_completion_ignores_superseded_links():
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")
    assert "superseded_by IS NULL" in source
    assert "async def _active_milestone_links" in source
    # 링크가 없는 마일스톤은 완료되지 않는다 (조기 자동진행 금지)
    assert '"reason": "no_linked_tasks"' in source


def test_goal_progress_gives_partial_credit_without_premature_completion():
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")
    assert "async def _in_progress_milestone_credit" in source
    # 부분 점수는 0.99 를 넘지 못한다 — 1.0/completed 는 전 마일스톤 완료로만 도달
    assert "0.99" in source
    assert "total > 0 and total == completed" in source
