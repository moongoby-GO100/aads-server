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
