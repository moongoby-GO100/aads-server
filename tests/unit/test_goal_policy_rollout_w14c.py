from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.goal_policy_rollout import evaluate_shadow_policy, is_permission_widening

ROOT = Path(__file__).parents[2]
MIGRATION = (ROOT / "migrations/20260919_goal_policy_rollout_w14c.sql").read_text()


def decision(**overrides):
    value = {
        "project": "AADS", "target_type": "task", "action": "update",
        "environment": "dev", "risk_tier": "A1", "boundary_decision": "ALLOW",
    }
    value.update(overrides)
    return value


def test_t27_shadow_rules_are_deterministic_and_non_mutating():
    policy = {"default_result": "DENY", "rules": [
        {"when": {"action": ["update"], "environment": "dev"}, "result": "APPROVAL_REQUIRED"}
    ]}
    result, reasons = evaluate_shadow_policy(policy, decision())
    assert result == "APPROVAL_REQUIRED"
    assert reasons == ["shadow_rule:0"]


def test_permission_widening_blocks_auto_expansion():
    assert is_permission_widening("APPROVAL_REQUIRED", "AUTO") is True
    assert is_permission_widening("AUTO", "APPROVAL_REQUIRED") is False
    assert "widened_count" in MIGRATION
    assert "policy promotion requires a non-widening simulation" in MIGRATION


def test_explicit_deny_and_mandatory_human_are_preserved():
    allow = {"default_result": "AUTO"}
    assert evaluate_shadow_policy(allow, decision(boundary_decision="DENY"))[0] == "DENY"
    result, reasons = evaluate_shadow_policy(
        allow, decision(environment="production", action="production_deploy", risk_tier="A3")
    )
    assert result == "APPROVAL_REQUIRED"
    assert "mandatory_human_preserved" in reasons


def test_unknown_policy_fields_fail_closed():
    with pytest.raises(HTTPException) as exc:
        evaluate_shadow_policy(
            {"rules": [{"when": {"tenant_secret": "x"}, "result": "AUTO"}]}, decision()
        )
    assert exc.value.detail["code"] == "invalid_policy_match_field"


def test_t28_shadow_store_contains_hashes_not_raw_context():
    assert "decision_input_hash" in MIGRATION
    assert "input_context" not in MIGRATION
    assert "goal_policy_simulation_results_append_only" in MIGRATION
    assert "goal_policy_rollout_events_append_only" in MIGRATION
