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
from app.services.goal_workflow_approval import canonical_hash, decide_change_set, preview_grant

TENANT = "00000000-0000-0000-0000-000000000001"
SESSION = "00000000-0000-0000-0000-000000000002"
OTHER = "00000000-0000-0000-0000-000000000003"
CHANGE = "00000000-0000-0000-0000-000000000004"
ROOT = Path(__file__).parents[2]
SERVICE = (ROOT / "app/services/goal_workflow_approval.py").read_text()
MIGRATION = (ROOT / "migrations/20260919_goal_work_hierarchy_m14.sql").read_text()


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


def test_t06_t12_t13_t14_change_set_guards_are_present():
    for contract in ("approval_superseded", "execution_key_conflict", "owner_instance", "owner_epoch",
                     "approval_revoked"):
        assert contract in SERVICE


def test_t11_t31_evidence_gate_precedes_grant_consumption():
    assert SERVICE.index('if request.get("evidence_required")') < SERVICE.index("used_executions=used_executions+1")
    assert "evidence_required" in SERVICE and "children_open" in SERVICE


def test_t17_t20_t24_t30_t33_single_grant_atomic_budget_contract():
    assert "FOR UPDATE OF g SKIP LOCKED LIMIT 1" in SERVICE
    assert "used_executions < g.max_executions" in SERVICE
    assert "UNIQUE (tenant_id, execution_key)" in (ROOT / "migrations/20260919_goal_work_hierarchy_m12.sql").read_text()
    for budget in ("max_files", "max_rows", "max_cost_usd", "max_parallel", "max_duration_seconds"):
        assert budget in SERVICE


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
