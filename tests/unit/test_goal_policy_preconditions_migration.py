from pathlib import Path


ROOT = Path(__file__).parents[2]
SQL = (ROOT / "migrations/20260919_goal_policy_preconditions_w13.sql").read_text()


def test_w13_stores_are_additive_repeatable_and_tenant_fenced():
    assert "CREATE TABLE IF NOT EXISTS goal_precondition_snapshots" in SQL
    assert "CREATE TABLE IF NOT EXISTS goal_policy_inputs" in SQL
    assert "IF NOT EXISTS (SELECT 1 FROM pg_policies" in SQL
    assert SQL.count("ENABLE ROW LEVEL SECURITY") == 2
    assert SQL.count("FORCE ROW LEVEL SECURITY") == 2
    assert "DROP TABLE" not in SQL
    assert "TRUNCATE" not in SQL


def test_w13_persists_version_hash_identity_and_no_auto_result():
    for field in (
        "snapshot_version", "target_version", "snapshot_hash",
        "evidence_snapshot_hash", "workspace_kind", "principal_session_id",
        "coordinator_session_id", "assignment_id", "patch_hash", "policy_version",
    ):
        assert field in SQL
    assert "AUTO" not in SQL
    assert "ux_project_role_assignments_principal_scope" in SQL
    assert "fk_goal_policy_input_principal_assignment" in SQL
    assert "ALTER TABLE milestones ADD COLUMN IF NOT EXISTS version" in SQL
    assert "CREATE OR REPLACE TRIGGER trg_milestone_version" in SQL
