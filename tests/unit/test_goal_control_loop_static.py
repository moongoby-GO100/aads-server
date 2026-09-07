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


def test_in_flight_goals_record_preservation_policy_evidence() -> None:
    """시드/마이그레이션으로 만들어진 진행 중 목표도 보존정책 증거를 남겨야 한다."""
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")

    # create_goal 한 곳에만 있으면 create_goal을 거치지 않은 목표는 증거가 0건이다.
    assert source.count("_trace_policy(") >= 4
    assert 'stage="activate"' in source
    assert 'stage="advance"' in source
    assert '"goal_policy"' in source


def test_activate_goal_keeps_single_connection_check_then_update() -> None:
    """활성화 검사→갱신은 기존대로 커넥션 1개 안에서 끝나야 한다 (TOCTOU 방지)."""
    source = (ROOT / "app" / "services" / "goal_manager.py").read_text(encoding="utf-8")
    body = source.split("async def activate_goal", 1)[1].split("\n    async def ", 1)[0]

    assert body.count("pool.acquire()") == 1, "activate_goal은 커넥션을 두 번 잡으면 안 된다"
    assert 'return {"error": "goal_not_found"}' in body
    assert 'return {"error": rejection}' in body
    # 점유한 커넥션을 trace에 그대로 넘겨 중첩 acquire를 피한다.
    assert body.count("conn=conn") >= 2


def test_pipeline_runner_records_preservation_audit_traces() -> None:
    source = (ROOT / "app" / "services" / "pipeline_runner_service.py").read_text(encoding="utf-8")

    assert "async def _trace_runner(" in source
    assert "async def _trace_task_policy(" in source
    assert "async def _trace_preservation_audit(" in source
    assert "from app.services.ohvis_harness_trace import record_trace" in source
    assert "await self._trace_task_policy()" in source
    assert "await self._trace_preservation_audit(review)" in source
    for run_type in ('"runner_policy"', '"runner_preservation_audit"'):
        assert run_type in source, f"러너 trace 미배선: {run_type}"

    # 판정 기준은 code_reviewer 보존 게이트 헬퍼를 재사용한다 (중복 구현 금지).
    assert "from app.services.code_reviewer import (" in source
    for helper in ("_diff_line_counts", "_extract_changed_files", "_extract_instruction_paths"):
        assert helper in source, f"code_reviewer 헬퍼 재사용 누락: {helper}"

    # 추적 실패는 러너를 막지 않는다.
    assert 'logger.debug("runner_trace_skipped' in source
    assert 'logger.debug("runner_preservation_trace_skipped' in source


def test_goal_schema_has_idempotent_task_link_index() -> None:
    sql = (ROOT / "migrations" / "157_goal_control_loop_dedup_and_advance.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_task_links_goal_task" in sql
    assert "ON goal_task_links(goal_id, task_type, task_id)" in sql
