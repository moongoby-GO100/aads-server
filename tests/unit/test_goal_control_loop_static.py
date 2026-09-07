from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_goals_api_exposes_timeline_control_endpoints() -> None:
    source = (ROOT / "app" / "routers" / "goals.py").read_text(encoding="utf-8")

    assert '@router.post("/goals/{goal_id}/advance")' in source
    assert '@router.post("/goals/advance")' in source
    assert '@router.post("/goals/task-status")' in source
    assert "milestones: Optional[list[MilestoneCreateRequest]]" in source


def test_goal_manager_links_tasks_idempotently_and_updates_terminal_statuses() -> None:
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")

    assert "_DONE_TASK_STATUSES" in source
    assert "_FAILED_TASK_STATUSES" in source
    assert "ON CONFLICT (goal_id, task_type, task_id)" in source
    assert "async def advance_goal" in source
    assert "async def advance_active_goals" in source
    assert "status = 'blocked'" in source


def test_pipeline_runner_updates_linked_goals_for_all_terminal_states() -> None:
    source = (ROOT / "app" / "services" / "pipeline_runner_service.py").read_text(encoding="utf-8")

    assert 'async def _update_linked_goal_state(job_id: str, status: str = "done")' in source
    assert "self.status in _TERMINAL_JOB_STATUSES" in source
    assert 'update_task_status("pipeline_job", job_id, status)' in source


def test_goal_manager_records_harness_traces_at_low_risk_points() -> None:
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")

    assert "async def _trace(" in source
    assert "from app.services.ohvis_harness_trace import record_trace" in source
    for run_type in (
        '"goal_create"',
        '"goal_activate"',
        '"milestone_add"',
        '"goal_task_link"',
        '"goal_task_status"',
        '"goal_advance"',
        '"goals_advance_sweep"',
    ):
        assert run_type in source, f"trace 미배선: {run_type}"

    # 추적은 비치명적이어야 한다 — 예외를 삼키고 debug 로그만 남긴다.
    assert "logger.debug(\"goal_trace_skipped" in source
    assert "compile_task_policy" in source


def test_goal_seed_migration_is_idempotent_and_never_completes_goals() -> None:
    sql = (ROOT / "migrations" / "160_goal_seed_chat_stability_and_ohvis_followup.sql").read_text(
        encoding="utf-8"
    )

    assert "채팅 시스템 안정화 및 응답 가독성 개선" in sql
    assert "오비스 자율 오케스트레이션 완성" in sql
    assert "'draft'" in sql, "후속 목표는 draft 로 시드해야 한다"

    # 4개 마일스톤을 sequence 1..4 로 시드한다.
    for sequence in (1, 2, 3, 4):
        assert f"({sequence}, '" in sql

    # 이미 있으면 다시 만들지 않고, 완료 처리는 하지 않는다.
    assert "IF v_goal_id IS NULL THEN" in sql
    assert "WHERE NOT EXISTS (" in sql
    assert "'completed'" not in sql
    assert "completed_at" not in sql


def test_goal_schema_has_idempotent_task_link_index() -> None:
    sql = (ROOT / "migrations" / "157_goal_control_loop_dedup_and_advance.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_task_links_goal_task" in sql
    assert "ON goal_task_links(goal_id, task_type, task_id)" in sql
