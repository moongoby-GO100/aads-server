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


def test_goal_schema_has_idempotent_task_link_index() -> None:
    sql = (ROOT / "migrations" / "157_goal_control_loop_dedup_and_advance.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_task_links_goal_task" in sql
    assert "ON goal_task_links(goal_id, task_type, task_id)" in sql
