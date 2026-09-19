from pathlib import Path


MIGRATION = (
    Path(__file__).parents[2]
    / "migrations"
    / "20260919_seed_goal_system_admin_role.sql"
)


def test_goal_system_admin_role_migration_is_scoped_and_idempotent():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "'role-goal-system-admin'" in sql
    assert "'GoalSystemAdmin'" in sql
    assert "ARRAY['AADS']" in sql
    assert "ON CONFLICT (slug) DO UPDATE" in sql
    assert "ON CONFLICT (role) DO UPDATE" in sql
    assert "포트폴리오" in sql
    assert "project_local" in sql


def test_goal_system_admin_role_migration_is_non_destructive():
    sql = MIGRATION.read_text(encoding="utf-8").upper()

    assert "DROP TABLE" not in sql
    assert "TRUNCATE" not in sql
    assert "DELETE FROM" not in sql
