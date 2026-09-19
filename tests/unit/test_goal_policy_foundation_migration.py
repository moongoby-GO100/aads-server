from pathlib import Path


ROOT = Path(__file__).parents[2]
UP = (ROOT / "migrations/20260919_goal_policy_foundation_stores.sql").read_text()
DOWN = (ROOT / "migrations/rollback/20260919_goal_policy_foundation_stores.down.sql").read_text()
VERIFY = (ROOT / "migrations/20260919_goal_policy_foundation_stores.verify.sql").read_text()
ADR = (ROOT / "docs/decisions/ADR-021-goal-policy-tenant-isolation.md").read_text()


STORES = (
    "work_item_dependencies",
    "work_item_evidence",
    "goal_kill_switches",
    "goal_policy_decisions",
    "goal_workflow_outbox",
    "goal_execution_leases",
    "goal_auto_approval_use_reservations",
    "goal_auto_approval_use_events",
    "work_item_review_requirements",
    "work_item_review_decisions",
)


def test_all_ten_prd_stores_are_created_or_hardened():
    for store in STORES:
        assert store in UP
    for new_store in STORES[2:4] + STORES[5:]:
        assert f"CREATE TABLE IF NOT EXISTS {new_store}" in UP
    assert "ALTER TABLE work_item_dependencies" in UP
    assert "ALTER TABLE work_item_evidence" in UP
    assert "ALTER TABLE goal_workflow_outbox" in UP


def test_foundation_hash_epoch_review_and_reconciliation_contracts():
    for field in (
        "decision_input_hash", "precondition_snapshot_hash", "policy_version",
        "grant_version", "canonicalization_version", "diagnostics", "erased",
        "masked", "payload_hash", "sequence_no", "claim_token", "publish_state",
        "owner_epoch", "reconciliation_state", "minimum_approvals", "sla_seconds",
        "override_reason", "risk_acceptance",
    ):
        assert field in UP
    assert "kill switch epoch must increase" in UP
    assert "aads_forbid_append_only_mutation" in UP
    assert "CREATE OR REPLACE VIEW goal_approval_kill_switches_compat" in UP
    assert "CREATE OR REPLACE VIEW goal_approval_decision_logs_compat" in UP
    assert "security_invoker=true" in UP


def test_tenant_isolation_is_default_deny_and_rollback_is_non_destructive():
    assert "aads_current_tenant_id()" in UP
    assert "ENABLE ROW LEVEL SECURITY" in UP
    assert "FORCE ROW LEVEL SECURITY" in UP
    assert "CREATE POLICY tenant_isolation" in UP
    assert "DROP TABLE" not in UP
    assert "TRUNCATE" not in UP
    assert "DROP TABLE" not in DOWN
    assert "DISABLE ROW LEVEL SECURITY" not in DOWN
    integration = (ROOT / "tests/integration/test_goal_policy_foundation_migration.py").read_text()
    assert "DROP SCHEMA" not in integration
    assert "must identify a fresh empty test database" in integration
    assert "_fresh_database_variant(M12)" in integration
    assert "_fresh_database_variant(M14)" in integration
    assert "NOSUPERUSER NOBYPASSRLS" in ADR
    assert "transaction-local" in ADR
    assert "relrowsecurity AND relforcerowsecurity" in VERIFY
    assert "one or more append-only triggers are missing" in VERIFY
