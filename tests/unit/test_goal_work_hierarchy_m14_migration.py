from pathlib import Path

ROOT = Path(__file__).parents[2]
UP = (ROOT / "migrations/20260919_goal_work_hierarchy_m14.sql").read_text()
DOWN = (ROOT / "migrations/rollback/20260919_goal_work_hierarchy_m14.down.sql").read_text()


def test_m14_migration_is_additive_repeatable_and_rollback_exists():
    assert "CREATE TABLE IF NOT EXISTS goal_approval_decision_logs" in UP
    assert "CREATE TABLE IF NOT EXISTS goal_approval_kill_switches" in UP
    assert "CREATE TABLE IF NOT EXISTS goal_workflow_outbox" in UP
    assert "ADD COLUMN IF NOT EXISTS owner_instance" in UP
    assert "CREATE INDEX IF NOT EXISTS" in UP
    assert "DROP TABLE IF EXISTS goal_approval_decision_logs" in DOWN


def test_decisions_are_append_only_and_assignment_policy_changes_stale_grants():
    assert "approval decision logs are append-only" in UP
    assert "AFTER UPDATE OF active ON project_role_assignments" in UP
    assert "AFTER INSERT ON goal_approval_policy_versions" in UP
    assert "status='stale'" in UP
