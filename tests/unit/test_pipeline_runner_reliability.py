from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_pipeline_jobs_list_uses_optional_auth_recovery_columns():
    source = (ROOT / "app/api/pipeline_runner.py").read_text()

    assert "_pipeline_column_exists" in source
    assert "auth_recovery_state" in source
    assert "auth_recovery_metadata" in source
    assert "NULL::text" in source
    assert "NULL::jsonb" in source


def test_admin_task_board_uses_auth_state_fallback():
    source = (ROOT / "app/api/admin.py").read_text()

    assert "_task_board_status_sql" in source
    assert "auth_recovery_pending" in source
    assert "awaiting_user_auth" in source
    assert "NULL::text" in source


def test_pipeline_runner_approval_requires_review_and_commit_gate():
    api = (ROOT / "app/api/pipeline_runner.py").read_text()
    # 판정 로직은 사전검증과 공유하는 서비스 한 곳에 있고, 승인 API 는 그것을 호출한다.
    gates = (ROOT / "app/services/runner_approval_precheck.py").read_text()

    assert "승인 차단: 유효한 git diff가 없습니다" in gates
    assert "승인 차단: 승인용 commit_hash가 없습니다" in gates
    assert "승인 차단: 실제 변경 파일 목록이 없습니다" in gates
    assert "승인 차단: AI 리뷰 결과가 없습니다" in gates
    assert "review[\"verdict\"] != \"APPROVE\"" in gates
    assert "evaluate_approval_gates(" in api
    assert "승인 차단:" not in api
    assert "'event', 'approval_decision'" in api


def test_pipeline_runner_exposes_model_stats_and_approval_timestamps():
    source = (ROOT / "app/api/pipeline_runner.py").read_text()

    assert '"/pipeline/runner/model-stats"' in source
    assert "pipeline_runner_events" in source
    assert "avg_attempt_seconds" in source
    assert "done_rate_pct" in source
    assert "approved_at = NOW()" in source
    assert "rejected_at = NOW()" in source
