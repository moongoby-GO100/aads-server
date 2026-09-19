from pathlib import Path

ROOT = Path(__file__).parents[2]
UP = (ROOT / "migrations/20260919_goal_work_hierarchy_m12.sql").read_text()
DOWN = (ROOT / "migrations/rollback/20260919_goal_work_hierarchy_m12.down.sql").read_text()
SECURITY = (ROOT / "migrations/20260919_goal_work_hierarchy_m14_security.sql").read_text()


def test_m12_tables_remain_present_after_m14_router_is_added():
    for table in (
        "work_items", "work_item_dependencies", "work_item_evidence",
        "project_role_assignments", "work_item_change_sets", "work_item_events",
        "goal_auto_approval_grants", "goal_auto_approval_uses",
        "goal_approval_policy_versions",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in UP
    # M12 originally shipped schema-only; M14 now owns this dedicated router.
    assert (ROOT / "app/routers/work_items.py").exists()


def test_work_item_scope_status_and_idempotency_contract():
    assert "tenant_id UUID NOT NULL" in UP
    assert "('draft','ready','in_progress','in_review','completed','blocked','cancelled','changes_requested')" in UP
    assert "UNIQUE NULLS NOT DISTINCT (tenant_id, project, parent_id, idempotency_key)" in UP
    assert "fk_work_item_goal_scope" in UP
    assert "fk_work_item_milestone_scope" in UP
    assert "fk_work_item_parent_scope" in UP
    assert "trg_milestone_scope" in UP
    assert "idempotency_key TEXT UNIQUE" not in UP


def test_assignment_uniqueness_and_graph_guards_are_database_enforced():
    assert "ON project_role_assignments (tenant_id, project, role_key) WHERE active" in UP
    assert "ON project_role_assignments (tenant_id, project, session_id) WHERE active" in UP
    assert "pg_advisory_xact_lock" in UP
    assert "work item parent cycle" in UP
    assert "work item dependency cycle" in UP
    assert "ck_work_item_dependency_not_self" in UP
    assert "parent scope mismatch" in UP


def test_governance_constraints_are_fail_closed():
    assert "used_executions >= 0 AND used_executions <= max_executions" in UP
    assert "max_risk_tier IN ('A0','A1','A2')" in UP
    assert "issued_by <> principal_session_id" in UP
    assert "ck_goal_grant_no_production" in UP
    assert "ck_goal_grant_no_mandatory_human_action" in UP
    assert "UNIQUE (tenant_id, execution_key)" in UP
    assert "trg_work_item_change_set_immutable" in UP
    assert "trg_goal_active_grant_immutable" in UP
    assert "trg_work_item_events_append_only" in UP
    assert "trg_goal_policy_versions_immutable" in UP


def test_migration_is_repeatable_and_has_explicit_empty_schema_rollback():
    assert "CREATE TABLE IF NOT EXISTS" in UP
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in UP
    assert "DROP TRIGGER IF EXISTS" in UP
    for table in ("work_items", "project_role_assignments", "goal_auto_approval_grants"):
        assert f"DROP TABLE IF EXISTS {table}" in DOWN


def test_m14_security_environment_migration_is_additive_and_idempotent():
    assert "ADD COLUMN IF NOT EXISTS environment" in SECURITY
    assert "CHECK (environment IN ('dev', 'staging', 'production'))" in SECURITY
    assert "pg_constraint" in SECURITY
