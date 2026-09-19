from pathlib import Path

SQL = (
    Path(__file__).parents[2]
    / "migrations/20260919_goal_task_links_tenant_integrity.sql"
).read_text()


def test_goal_task_links_tenant_migration_is_additive_and_fail_closed():
    assert "ADD COLUMN IF NOT EXISTS tenant_id UUID" in SQL
    assert "goal_task_links tenant backfill incomplete" in SQL
    assert "ALTER COLUMN tenant_id SET NOT NULL" in SQL
    assert "fk_goal_task_links_goal_tenant" in SQL
    assert "fk_goal_task_links_milestone_tenant" in SQL
    assert "trg_goal_task_links_tenant" in SQL
    assert "goal_task_links tenant mismatch" in SQL
    assert "DROP TABLE" not in SQL.upper()
    assert "TRUNCATE" not in SQL.upper()


def test_goal_task_links_tenant_migration_is_idempotent():
    assert "ADD COLUMN IF NOT EXISTS" in SQL
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in SQL
    assert "CREATE INDEX IF NOT EXISTS" in SQL
    assert "IF NOT EXISTS" in SQL
