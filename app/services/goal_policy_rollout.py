"""W-14c server-side shadow replay and audited policy rollout."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.core.goal_work_hierarchy_policy import action_requires_mandatory_human
from app.services.goal_work_hierarchy import ActorScope

_RESULT_RANK = {"DENY": 0, "NOT_EXECUTABLE": 0, "APPROVAL_REQUIRED": 1, "AUTO": 2}
_RESULTS = frozenset(_RESULT_RANK)
_MATCH_FIELDS = frozenset({"project", "action", "environment", "risk_tier", "target_type"})


def _error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def evaluate_shadow_policy(policy: Mapping[str, Any], decision: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Evaluate the small, deterministic rollout DSL against a persisted decision."""
    default_result = str(policy.get("default_result", "NOT_EXECUTABLE"))
    if default_result not in _RESULTS:
        raise _error(422, "invalid_policy_default")
    selected = default_result
    reasons = ["shadow_default"]
    rules = policy.get("rules", [])
    if not isinstance(rules, list):
        raise _error(422, "invalid_policy_rules")
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping) or not isinstance(rule.get("when", {}), Mapping):
            raise _error(422, "invalid_policy_rule")
        conditions = rule.get("when", {})
        if set(conditions) - _MATCH_FIELDS:
            raise _error(422, "invalid_policy_match_field")
        matched = True
        for field, allowed in conditions.items():
            values = allowed if isinstance(allowed, list) else [allowed]
            if str(decision.get(field)) not in {str(value) for value in values}:
                matched = False
                break
        if matched:
            selected = str(rule.get("result", "NOT_EXECUTABLE"))
            if selected not in _RESULTS:
                raise _error(422, "invalid_policy_result")
            reasons = [f"shadow_rule:{index}"]
            break
    # Existing explicit denial and mandatory-human boundaries are authoritative.
    if str(decision.get("boundary_decision")) == "DENY":
        return "DENY", [*reasons, "explicit_deny_preserved"]
    if (str(decision.get("risk_tier")) == "A3" or action_requires_mandatory_human(
        str(decision.get("action", "")), str(decision.get("environment", "dev")), []
    )) and selected == "AUTO":
        selected = "APPROVAL_REQUIRED"
        reasons.append("mandatory_human_preserved")
    return selected, reasons


def is_permission_widening(baseline: str, shadow: str) -> bool:
    if baseline not in _RESULTS or shadow not in _RESULTS:
        raise ValueError("unknown policy result")
    return _RESULT_RANK[shadow] > _RESULT_RANK[baseline]


async def simulate_policy(
    conn: Any, *, tenant_id: str, actor: ActorScope, candidate_policy_id: str,
    decision_ids: Sequence[str],
) -> dict[str, Any]:
    if not decision_ids or len(decision_ids) > 500:
        raise _error(422, "invalid_simulation_sample")
    candidate = await conn.fetchrow(
        """SELECT id::text,project,policy,mode FROM goal_approval_policy_versions
             WHERE id=$1::uuid AND tenant_id=$2::uuid FOR UPDATE""",
        candidate_policy_id, tenant_id,
    )
    if not candidate or not actor.may_access(str(candidate["project"] or actor.project)):
        raise _error(404, "policy_not_found")
    if candidate["mode"] not in {"audit_only", "canary"}:
        raise _error(409, "policy_not_simulatable")
    policy = candidate["policy"]
    if isinstance(policy, str):
        policy = json.loads(policy)
    rows = await conn.fetch(
        """SELECT id::text,project,target_type,action,environment,risk_tier,boundary_decision,
                  result,decision_input_hash
             FROM goal_policy_decisions
            WHERE tenant_id=$1::uuid AND id=ANY($2::uuid[])
              AND ($3::text IS NULL OR project=$3)""",
        tenant_id, list(decision_ids), candidate["project"],
    )
    if len(rows) != len(set(decision_ids)):
        raise _error(404, "simulation_decision_not_found")
    run_id = str(uuid4())
    results: list[tuple[Any, str, bool, list[str]]] = []
    for row in rows:
        shadow, reasons = evaluate_shadow_policy(policy, row)
        baseline = str(row["result"])
        results.append((row, shadow, is_permission_widening(baseline, shadow), reasons))
    changed = sum(str(row["result"]) != shadow for row, shadow, _, _ in results)
    widened = sum(widened for _, _, widened, _ in results)
    summary = {"sample_count": len(results), "changed_count": changed, "widened_count": widened,
               "masked_input_values_persisted": 0}
    await conn.execute(
        """INSERT INTO goal_policy_simulation_runs
             (id,tenant_id,project,candidate_policy_id,requested_by,sample_count,
              changed_count,widened_count,summary)
           VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5::uuid,$6,$7,$8,$9::jsonb)""",
        run_id, tenant_id, candidate["project"] or actor.project, candidate_policy_id,
        actor.session_id, len(results), changed, widened, json.dumps(summary),
    )
    for row, shadow, expanded, reasons in results:
        await conn.execute(
            """INSERT INTO goal_policy_simulation_results
                 (tenant_id,project,run_id,historical_decision_id,decision_input_hash,
                  baseline_result,shadow_result,widened,reason_codes)
               VALUES($1::uuid,$2,$3::uuid,$4::uuid,$5,$6,$7,$8,$9::text[])""",
            tenant_id, row["project"], run_id, row["id"], row["decision_input_hash"],
            row["result"], shadow, expanded, reasons,
        )
    await conn.execute(
        """UPDATE goal_approval_policy_versions SET simulation_result=$3::jsonb
             WHERE id=$1::uuid AND tenant_id=$2::uuid""",
        candidate_policy_id, tenant_id, json.dumps({**summary, "run_id": run_id}),
    )
    return {"run_id": run_id, **summary, "promotion_eligible": widened == 0}


async def promote_policy(
    conn: Any, *, tenant_id: str, actor: ActorScope, policy_id: str,
    target_mode: str, reason: str,
) -> dict[str, Any]:
    if actor.workspace_kind != "ceo_integrated":
        raise _error(403, "ceo_approval_required")
    policy = await conn.fetchrow(
        """SELECT * FROM goal_approval_policy_versions
             WHERE id=$1::uuid AND tenant_id=$2::uuid FOR UPDATE""",
        policy_id, tenant_id,
    )
    if not policy or not actor.may_access(str(policy["project"] or actor.project)):
        raise _error(404, "policy_not_found")
    if target_mode not in {"canary", "enabled", "rollback"}:
        raise _error(422, "invalid_rollout_target")
    if target_mode == "rollback":
        rollback_id = policy["previous_version_id"]
        if policy["mode"] not in {"canary", "enabled"} or not rollback_id:
            raise _error(409, "rollback_unavailable")
        previous = await conn.fetchrow(
            "SELECT * FROM goal_approval_policy_versions WHERE id=$1::uuid AND tenant_id=$2::uuid FOR UPDATE",
            rollback_id, tenant_id,
        )
        if not previous:
            raise _error(409, "rollback_unavailable")
        await conn.execute(
            """UPDATE goal_approval_policy_versions SET mode='retired',approved_by=$3::uuid,
                      effective_at=clock_timestamp() WHERE id=$1::uuid AND tenant_id=$2::uuid""",
            policy_id, tenant_id, actor.session_id,
        )
        await conn.execute(
            """UPDATE goal_approval_policy_versions SET mode='enabled',approved_by=$3::uuid,
                      effective_at=clock_timestamp() WHERE id=$1::uuid AND tenant_id=$2::uuid""",
            str(rollback_id), tenant_id, actor.session_id,
        )
        destination, rollback_policy = "retired", str(rollback_id)
    else:
        expected = "audit_only" if target_mode == "canary" else "canary"
        if policy["mode"] != expected:
            raise _error(409, "invalid_rollout_transition")
        latest = await conn.fetchrow(
            """SELECT id::text,sample_count,widened_count,created_at
                 FROM goal_policy_simulation_runs
                WHERE tenant_id=$1::uuid AND candidate_policy_id=$2::uuid
                ORDER BY created_at DESC LIMIT 1""",
            tenant_id, policy_id,
        )
        if not latest or latest["widened_count"] != 0:
            raise _error(409, "permission_widening_detected")
        if target_mode == "enabled" and policy["effective_at"] and latest["created_at"] <= policy["effective_at"]:
            raise _error(409, "post_canary_simulation_required")
        await conn.execute(
            """UPDATE goal_approval_policy_versions SET mode=$3,approved_by=$4::uuid,
                      effective_at=clock_timestamp() WHERE id=$1::uuid AND tenant_id=$2::uuid""",
            policy_id, tenant_id, target_mode, actor.session_id,
        )
        await conn.execute(
            """UPDATE goal_auto_approval_grants SET status='stale',revoked_at=clock_timestamp(),
                      revoke_reason='stale_policy'
                WHERE tenant_id=$1::uuid AND status='active' AND policy_version<>$2::uuid
                  AND ($3::text IS NULL OR project=$3)""",
            tenant_id, policy_id, policy["project"],
        )
        destination, rollback_policy = target_mode, None
    event = await conn.fetchrow(
        """INSERT INTO goal_policy_rollout_events
             (tenant_id,project,policy_version_id,from_mode,to_mode,simulation_run_id,
              rollback_policy_id,decided_by,reason)
           VALUES($1::uuid,$2,$3::uuid,$4,$5,
             (SELECT id FROM goal_policy_simulation_runs WHERE tenant_id=$1::uuid
               AND candidate_policy_id=$3::uuid ORDER BY created_at DESC LIMIT 1),
             $6::uuid,$7::uuid,$8) RETURNING id::text,created_at""",
        tenant_id, policy["project"] or actor.project, policy_id, policy["mode"], destination,
        rollback_policy, actor.session_id, reason,
    )
    return {"policy_id": policy_id, "mode": destination, "rollback_policy_id": rollback_policy,
            "event_id": row_value(event, "id")}


def row_value(row: Mapping[str, Any], key: str) -> Any:
    return row[key]
