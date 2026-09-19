from __future__ import annotations

from pathlib import Path

from app.services.goal_policy_foundation import sanitize_context
from app.services.goal_policy_shadow import evaluate_shadow_policy, is_privilege_expansion

ROOT = Path(__file__).parents[2]
SOURCE = (ROOT / "app/services/goal_policy_shadow.py").read_text()
MIGRATION = (ROOT / "migrations/20260919_goal_workflow_w14c.sql").read_text()


def test_shadow_policy_defaults_fail_closed_and_matches_narrow_rules():
    assert evaluate_shadow_policy({}, {"action": "update"})["decision"] == "DENY"
    policy = {
        "default_decision": "PROJECT_APPROVAL",
        "rules": [{"match": {"project": "AADS", "action": "update"}, "decision": "AUTO"}],
    }
    assert evaluate_shadow_policy(policy, {"project": "AADS", "action": "update"})["decision"] == "AUTO"
    assert evaluate_shadow_policy(policy, {"project": "GO100", "action": "update"})["decision"] == "PROJECT_APPROVAL"


def test_mandatory_human_always_overrides_shadow_auto():
    result = evaluate_shadow_policy(
        {"default_decision": "AUTO"},
        {"action": "production_deploy", "environment": "production"},
    )
    assert result == {"decision": "CEO_APPROVAL", "reason_codes": ["mandatory_human"]}


def test_privilege_expansion_order_is_explicit():
    assert is_privilege_expansion("PROJECT_APPROVAL", "AUTO") is True
    assert is_privilege_expansion("AUTO", "PROJECT_APPROVAL") is False
    assert is_privilege_expansion("DENY", "DENY") is False


def test_secret_paths_are_masked_before_shadow_evidence():
    safe, erased, masked = sanitize_context({
        "token": "secret-token-value",
        "nested": {"password": "secret-password", "safe": "visible"},
    })
    rendered = str(safe)
    assert "secret-token-value" not in rendered
    assert "secret-password" not in rendered
    assert safe["nested"]["safe"] == "visible"
    assert erased or masked


def test_shadow_replay_has_no_operational_mutation_path():
    body = SOURCE[SOURCE.index("async def replay_policy"):SOURCE.index("async def promote_policy")]
    for forbidden in (
        "UPDATE goal_auto_approval_grants",
        "INSERT INTO goal_auto_approval_uses",
        "INSERT INTO work_item_events",
        "INSERT INTO goal_workflow_outbox",
    ):
        assert forbidden not in body
    assert "simulation_side_effect_detected" in body
    assert "operational_counts_before" in body


def test_audit_tables_are_append_only_and_promotion_is_gated():
    for table in (
        "goal_policy_simulation_runs",
        "goal_policy_simulation_items",
        "goal_policy_promotion_events",
    ):
        assert table in MIGRATION
        assert f"trg_{table}_append_only" in MIGRATION
    promote = SOURCE[SOURCE.index("async def promote_policy"):SOURCE.index("async def rollback_policy")]
    assert "privilege_expansion_count" in promote
    assert "insufficient_replay_evidence" in promote
    assert '{"audit_only": "canary", "canary": "enabled"}' in promote


def test_rollback_is_new_revision_and_stales_bad_policy_grants():
    body = SOURCE[SOURCE.index("async def rollback_policy"):]
    assert "INSERT INTO goal_approval_policy_versions" in body
    assert "revoke_reason='policy_rollback'" in body
    assert "goal_policy_promotion_events" in body
    assert "DELETE" not in body
