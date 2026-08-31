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
    source = (ROOT / "app/api/pipeline_runner.py").read_text()

    assert "승인 차단: 유효한 git diff가 없습니다" in source
    assert "승인 차단: 승인용 commit_hash가 없습니다" in source
    assert "승인 차단: 실제 변경 파일 목록이 없습니다" in source
    assert "승인 차단: AI 리뷰 결과가 없습니다" in source
    assert "latest_review[\"verdict\"] != \"APPROVE\"" in source
    assert "'event', 'approval_decision'" in source
