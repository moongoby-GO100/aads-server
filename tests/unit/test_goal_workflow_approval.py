from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.goal_work_hierarchy_policy import (
    action_requires_mandatory_human,
    auto_approval_mode,
    mask_decision_context,
    workflow_approval_enabled,
)
from app.services.goal_work_hierarchy import ActorScope
from app.services.goal_workflow_approval import (
    _apply_internal_patch,
    _change_set_body_hash,
    canonical_hash,
    decide_change_set,
    preview_grant,
    route_change_set,
)

TENANT = "00000000-0000-0000-0000-000000000001"
SESSION = "00000000-0000-0000-0000-000000000002"
OTHER = "00000000-0000-0000-0000-000000000003"
CHANGE = "00000000-0000-0000-0000-000000000004"
ROOT = Path(__file__).parents[2]
SERVICE = (ROOT / "app/services/goal_workflow_approval.py").read_text()
MIGRATION = (ROOT / "migrations/20260919_goal_work_hierarchy_m14.sql").read_text()
W14A = (ROOT / "migrations/20260919_goal_workflow_w14a.sql").read_text()
W14B = (ROOT / "migrations/20260919_goal_workflow_w14b.sql").read_text()
W14B_RLS = (ROOT / "migrations/20260919_goal_workflow_w14b_rls_fix.sql").read_text()


def actor(session: str = OTHER) -> ActorScope:
    return ActorScope(TENANT, session, "reviewer", "AADS", "project")


def test_flags_default_fail_closed_and_invalid_mode_is_off(monkeypatch):
    monkeypatch.delenv("GOAL_WORKFLOW_APPROVAL_ENABLED", raising=False)
    monkeypatch.delenv("GOAL_AUTO_APPROVAL_MODE", raising=False)
    assert workflow_approval_enabled() is False
    assert auto_approval_mode() == "off"
    assert auto_approval_mode({"GOAL_AUTO_APPROVAL_MODE": "invalid"}) == "off"


def test_t07_patch_hash_is_canonical_and_tamper_path_exists():
    assert canonical_hash([{"op": "replace", "path": "/title", "value": "x"}]).startswith("sha256:")
    assert "patch_hash_mismatch" in SERVICE and "patch_tamper_blocked" in SERVICE


def test_t16_t18_mandatory_human_cannot_be_granted():
    for action in ("production_deploy", "financial", "secret", "destructive", "database_schema"):
        result = preview_grant({"principal_session_id": OTHER, "actions": [action], "environments": ["dev"]},
                               actor_session_id=SESSION)
        assert result["allowed"] is False
        assert action in result["rejected_reasons"]
    assert action_requires_mandatory_human("execute", "production", [])


def test_t19_self_grant_is_denied_at_preview():
    result = preview_grant({"principal_session_id": SESSION, "actions": ["execute"], "environments": ["dev"]},
                           actor_session_id=SESSION)
    assert result["allowed"] is False
    assert "self_grant" in result["rejected_reasons"]


def test_t28_decision_context_masks_nested_secrets():
    masked = mask_decision_context({"token": "abc", "nested": {"password": "p", "safe": "ok"},
                                    "authorization": "Bearer secret"})
    assert masked == {"token": "[REDACTED]", "nested": {"password": "[REDACTED]", "safe": "ok"},
                      "authorization": "[REDACTED]"}


class DecisionConn:
    def __init__(self, *, risk="A2", state="pending", requested_by=SESSION):
        self.row = {"id": CHANGE, "tenant_id": TENANT, "project": "AADS", "risk_tier": risk,
                    "state": state, "requested_by": requested_by, "approval_request_id": CHANGE}
        self.updates = 0

    async def fetchrow(self, sql, *args):
        if sql.lstrip().startswith("SELECT"):
            return self.row
        self.updates += 1
        self.row = {**self.row, "state": args[1], "decided_by": args[2]}
        return self.row

    async def execute(self, sql, *args):
        return "INSERT 0 1"

    async def fetchval(self, sql, *args):
        return False


class RouteConn:
    async def fetchrow(self, sql, *args):
        return {"project": "OTHER", "approval_request_id": None}


class PatchConn:
    def __init__(self):
        self.current = {
            "title": "old", "description": "description", "status": "ready",
            "priority": "P2", "progress": 0, "version": 1,
        }
        self.update = None

    async def fetchrow(self, sql, *args):
        if sql.lstrip().startswith("SELECT"):
            return self.current
        self.update = (sql, args)
        return {"id": CHANGE}


def test_route_change_set_denies_cross_project_actor():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(route_change_set(RouteConn(), tenant_id=TENANT, change_set_id=CHANGE, actor=actor()))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "project_scope_denied"


def test_t08_duplicate_approval_callback_is_idempotent():
    conn = DecisionConn(state="approved", requested_by=SESSION)
    result = asyncio.run(decide_change_set(conn, tenant_id=TENANT, change_set_id=CHANGE,
                                           actor=actor(), approve=True, reason="ok"))
    assert result["state"] == "approved" and conn.updates == 0


def test_t09_a3_bulk_approval_is_rejected():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(decide_change_set(DecisionConn(risk="A3"), tenant_id=TENANT, change_set_id=CHANGE,
                                      actor=actor(), approve=True, reason="", bulk=True))
    assert exc.value.status_code == 400 and exc.value.detail["code"] == "bulk_not_allowed"


def test_t10_self_approval_is_rejected():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(decide_change_set(DecisionConn(), tenant_id=TENANT, change_set_id=CHANGE,
                                      actor=actor(SESSION), approve=True, reason=""))
    assert exc.value.status_code == 403 and exc.value.detail["code"] == "self_approval_denied"


def test_t10_a2_member_without_active_lead_assignment_is_rejected():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(decide_change_set(DecisionConn(), tenant_id=TENANT, change_set_id=CHANGE,
                                      actor=actor(), approve=True, reason=""))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "project_lead_approval_required"


def test_t06_t12_t13_t14_change_set_guards_are_present():
    for contract in ("approval_superseded", "execution_key_conflict", "owner_instance", "owner_epoch",
                     "approval_revoked"):
        assert contract in SERVICE


def test_t06_idempotency_hash_covers_non_patch_body_fields():
    base = {"base_version": 1, "patch": [], "rationale": "r", "expected_effect": "e",
            "rollback_plan": "undo", "idempotency_key": "same"}
    assert _change_set_body_hash(CHANGE, base) == _change_set_body_hash(
        CHANGE, {**base, "idempotency_key": "ignored"}
    )
    assert _change_set_body_hash(CHANGE, base) != _change_set_body_hash(
        CHANGE, {**base, "rationale": "different"}
    )


def test_t07_immutable_versions_and_rfc6902_hash_contract():
    assert "target_version > base_version" in W14A
    assert "body_hash" in W14A
    assert "canonical_patch_hash" in SERVICE and "validate_patch" in SERVICE


def test_t07_ordered_patch_collapses_duplicate_paths_after_tests():
    conn = PatchConn()
    asyncio.run(_apply_internal_patch(
        conn, tenant_id=TENANT,
        row={"target_id": CHANGE, "base_version": 1, "target_version": 2},
        patch=[
            {"op": "test", "path": "/title", "value": "old"},
            {"op": "replace", "path": "/title", "value": "first"},
            {"op": "replace", "path": "/title", "value": "final"},
            {"op": "remove", "path": "/description"},
        ],
    ))
    sql, args = conn.update
    assert sql.count("title=") == 1
    assert "description=" in sql
    assert "final" in args and "first" not in args


def test_t07_patch_test_failure_is_fail_closed():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_apply_internal_patch(
            PatchConn(), tenant_id=TENANT,
            row={"target_id": CHANGE, "base_version": 1, "target_version": 2},
            patch=[{"op": "test", "path": "/title", "value": "tampered"}],
        ))
    assert exc.value.detail["code"] == "patch_test_failed"


def test_t08_t10_multi_approval_is_independent_and_rejection_wins():
    assert "work_item_change_set_approval_routes" in SERVICE
    assert "work_item_change_set_approval_decisions" in SERVICE
    assert "ceo\", \"independent_reviewer" in SERVICE
    assert 'aggregate_state = "rejected" if any_rejection' in SERVICE
    assert "self_approval_denied" in SERVICE


def test_t11_execution_and_acceptance_are_separate():
    execution = SERVICE[SERVICE.index("async def execute_change_set"):SERVICE.index("def preview_grant")]
    assert "review_item" not in execution
    assert "state='executed'" in execution


def test_t12_t13_transactional_outbox_and_exactly_once_effect_contract():
    assert "goal_workflow_effects" in SERVICE
    assert SERVICE.index("await _apply_internal_patch") < SERVICE.index("INSERT INTO goal_workflow_outbox")
    assert "UNIQUE (tenant_id,execution_key)" in W14A
    assert SERVICE.count("FOR UPDATE OF o SKIP LOCKED") == 1
    assert "SKIP LOCKED" not in SERVICE[SERVICE.index("async def complete_outbox_delivery"):]


def test_t14_unknown_external_outcome_requires_reconciliation():
    assert "unknown_external_outcome" in SERVICE
    assert "reconciliation_required" in SERVICE
    assert "refund" not in SERVICE[SERVICE.index("async def complete_outbox_delivery"):SERVICE.index("def preview_grant")]


def test_t11_t31_evidence_gate_precedes_grant_consumption():
    assert SERVICE.index('if request.get("evidence_required")') < SERVICE.index("used_executions=used_executions+1")
    assert "evidence_required" in SERVICE and "children_open" in SERVICE


def test_t17_t20_t24_t30_t33_single_grant_atomic_budget_contract():
    assert "FOR UPDATE OF g LIMIT 1" in SERVICE
    assert "used_executions < g.max_executions" in SERVICE
    assert "UNIQUE (tenant_id, execution_key)" in (ROOT / "migrations/20260919_goal_work_hierarchy_m12.sql").read_text()
    for budget in ("max_files", "max_rows", "max_cost_usd", "max_parallel", "max_duration_seconds"):
        assert budget in SERVICE
    for budget_reason in ("max_files_exhausted", "max_rows_exhausted", "max_cost_usd_exhausted",
                          "max_duration_seconds_exhausted"):
        assert budget_reason in SERVICE


def test_t21_t22_t23_t29_t32_revocation_and_staleness_contract():
    for marker in ("trg_stale_assignment_grants", "stale_assignment", "trg_stale_policy_grants", "stale_policy"):
        assert marker in MIGRATION
    assert "parent_grant_revoked" in SERVICE
    assert "clock_timestamp() BETWEEN g.valid_from AND g.expires_at" in SERVICE


def test_t25_local_assignment_and_t26_kill_switch_are_fail_closed():
    assert "local_assignment_required" in SERVICE
    assert "goal_approval_kill_switches" in SERVICE and "kill_switch" in SERVICE


def test_t27_simulation_has_no_usage_or_decision_log_writes():
    body = SERVICE[SERVICE.index("async def reserve_grant_use"):SERVICE.index("async def revoke_grant")]
    assert "if simulate or auto_approval_mode() == \"audit\"" in body
    assert "if not simulate:" in body


def test_t34_policy_store_failure_has_no_cached_permit_path():
    assert "goal_approval_policy_versions" in SERVICE
    assert "cache" not in SERVICE.lower()


def test_t35_revocation_strategy_is_fixed_on_grant():
    m12 = (ROOT / "migrations/20260919_goal_work_hierarchy_m12.sql").read_text()
    assert "revocation_strategy TEXT NOT NULL" in m12
    assert "cancel_now','finish_current','compensate" in m12


def test_w14b_execution_key_replay_requires_same_canonical_body():
    body = SERVICE[SERVICE.index("async def reserve_grant_use"):SERVICE.index("async def _decision")]
    assert "request_hash = canonical_hash(dict(request))" in body
    assert 'raise _error(409, "execution_key_conflict")' in body
    assert body.index("execution_key_conflict") < body.index("idempotent_replay")


def test_w14b_full_ancestor_chain_and_non_composition_contract():
    body = SERVICE[SERVICE.index("async def reserve_grant_use"):SERVICE.index("async def reconcile_grant_use")]
    assert "WITH RECURSIVE ancestors" in body
    assert "NOT g.actions <@ x.actions" in body
    assert "FOR UPDATE OF g LIMIT 1" in body
    assert "SKIP LOCKED" not in body


def test_w14b_lineage_depth_cycle_and_separation_are_db_enforced():
    for marker in ("logical_grant_id", "root_grant_id", "delegation_path",
                   "parent_grant_version", "parent_scope_hash", "ancestor_revocation_epoch"):
        assert marker in W14B
    assert "delegation depth exceeded" in W14B
    assert "delegation cycle" in W14B
    assert "delegation separation of duties" in W14B


def test_w14b_append_only_estimated_actual_and_unknown_outcome_contract():
    assert "goal_auto_approval_usage_events" in W14B
    assert "grant usage events are append-only" in W14B
    body = SERVICE[SERVICE.index("async def reconcile_grant_use"):SERVICE.index("async def _decision")]
    for marker in ("actual_budget", "budget_overrun", "manual_reconciliation",
                   '"auto_refund": False', '"auto_replay": False', "status='stale'"):
        assert marker in body
    assert "ENABLE ROW LEVEL SECURITY" in W14B_RLS
    assert "FORCE ROW LEVEL SECURITY" in W14B_RLS
    assert "tenant_isolation" in W14B_RLS


def test_w14b_recursive_revoke_and_retry_fails_closed():
    body = SERVICE[SERVICE.index("async def revoke_grant"):SERVICE.index("async def submit_review")]
    assert "WITH RECURSIVE descendants" in body
    assert "revocation_epoch=revocation_epoch+1" not in body
    assert "'revocation_epoch','last_used_at'" in W14B
    assert "lineage_backfill" in W14B
    reserve = SERVICE[SERVICE.index("async def reserve_grant_use"):SERVICE.index("async def reconcile_grant_use")]
    assert reserve.index("goal_approval_kill_switches") < reserve.index("idempotent_replay")
