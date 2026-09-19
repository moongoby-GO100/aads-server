"""W-14c side-effect-free policy simulation and immutable shadow evidence."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.core.goal_work_hierarchy_policy import action_requires_mandatory_human
from app.services.goal_policy_foundation import canonical_hash, sanitize_context
from app.services.goal_work_hierarchy import ActorScope

_DECISIONS = {"AUTO", "NOTIFY", "PROJECT_APPROVAL", "CEO_APPROVAL", "DENY"}
_DECISION_RANK = {"DENY": 0, "CEO_APPROVAL": 1, "PROJECT_APPROVAL": 2, "NOTIFY": 3, "AUTO": 4}
_MATCH_FIELDS = {"project", "action", "environment", "target_type", "risk_tier"}


def _error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _rule_matches(rule: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    match = rule.get("match") or {}
    if not isinstance(match, Mapping) or set(match).difference(_MATCH_FIELDS):
        return False
    for field, expected in match.items():
        actual = str(context.get(field, ""))
        if isinstance(expected, Sequence) and not isinstance(expected, str):
            if actual not in {str(value) for value in expected}:
                return False
        elif actual != str(expected):
            return False
    return True


def evaluate_shadow_policy(policy: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate the narrow JSON shadow contract; malformed input fails closed."""
    default = str(policy.get("default_decision", "DENY")).upper()
    if default not in _DECISIONS:
        default = "DENY"
    decision = default
    reasons = ["shadow_default"]
    rules = policy.get("rules") or []
    if not isinstance(rules, list):
        return {"decision": "DENY", "reason_codes": ["invalid_shadow_policy"]}
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping) or not _rule_matches(rule, context):
            continue
        candidate = str(rule.get("decision", "DENY")).upper()
        if candidate not in _DECISIONS:
            return {"decision": "DENY", "reason_codes": ["invalid_shadow_policy"]}
        decision = candidate
        reasons = [f"shadow_rule_{index}"]
        break
    if decision == "AUTO" and action_requires_mandatory_human(
        str(context.get("action", "")), str(context.get("environment", "dev")),
        context.get("risk_factors") or [],
    ):
        return {"decision": "CEO_APPROVAL", "reason_codes": ["mandatory_human"]}
    return {"decision": decision, "reason_codes": reasons}


def is_privilege_expansion(actual: str, candidate: str) -> bool:
    return _DECISION_RANK.get(candidate, 0) > _DECISION_RANK.get(actual, 0)


async def _operational_counts(conn: Any, tenant_id: str) -> dict[str, int]:
    row = await conn.fetchrow(
        """SELECT
             (SELECT count(*) FROM goal_auto_approval_uses WHERE tenant_id=$1::uuid) AS uses,
             (SELECT count(*) FROM work_item_events WHERE tenant_id=$1::uuid) AS events,
             (SELECT count(*) FROM goal_workflow_outbox WHERE tenant_id=$1::uuid) AS outbox""",
        tenant_id,
    )
    return {name: int(row[name]) for name in ("uses", "events", "outbox")}


async def replay_policy(
    conn: Any, *, tenant_id: str, policy_id: str, actor: ActorScope,
    limit: int = 200, run_mode: str = "historical_replay",
) -> dict[str, Any]:
    """Replay masked historical decisions without consuming grants or emitting work."""
    if run_mode not in {"simulate", "shadow", "historical_replay"}:
        raise _error(422, "invalid_simulation_mode")
    candidate = await conn.fetchrow(
        """SELECT * FROM goal_approval_policy_versions
             WHERE tenant_id=$1::uuid AND id=$2::uuid""",
        tenant_id, policy_id,
    )
    if not candidate:
        raise _error(404, "policy_not_found")
    project = candidate["project"]
    if (project and not actor.may_access(str(project))) or (not project and actor.workspace_kind != "ceo_integrated"):
        raise _error(404, "policy_not_found")
    baseline = await conn.fetchrow(
        """SELECT id::text FROM goal_approval_policy_versions
            WHERE tenant_id=$1::uuid AND id<>$2::uuid
              AND project IS NOT DISTINCT FROM $3
              AND mode IN ('canary','enabled')
            ORDER BY effective_at DESC NULLS LAST,created_at DESC LIMIT 1""",
        tenant_id, policy_id, project,
    )
    rows = await conn.fetch(
        """SELECT id::text,decision,input_context,created_at
             FROM goal_approval_decision_logs
            WHERE tenant_id=$1::uuid
              AND ($2::text IS NULL OR input_context->>'project'=$2)
            ORDER BY created_at DESC LIMIT $3""",
        tenant_id, project, max(1, min(int(limit), 1000)),
    )
    before = await _operational_counts(conn, tenant_id)
    policy = _json(candidate["policy"])
    items: list[dict[str, Any]] = []
    for row in rows:
        context = _json(row["input_context"] or {})
        safe_context, erased, masked = sanitize_context(context)
        shadow = evaluate_shadow_policy(policy, safe_context)
        actual = str(row["decision"])
        proposed = shadow["decision"]
        items.append({
            "source_decision_id": row["id"],
            "decision_input_hash": canonical_hash(safe_context),
            "actual_decision": actual,
            "candidate_decision": proposed,
            "divergence": actual != proposed,
            "privilege_expansion": is_privilege_expansion(actual, proposed),
            "masked_context": safe_context,
            "erased": erased,
            "masked": masked,
            "reason_codes": shadow["reason_codes"],
        })
    after = await _operational_counts(conn, tenant_id)
    if before != after:
        raise _error(409, "simulation_side_effect_detected")
    metrics = {
        "input_count": len(items),
        "divergence_count": sum(item["divergence"] for item in items),
        "privilege_expansion_count": sum(item["privilege_expansion"] for item in items),
        "deny_allow_divergence_count": sum(
            {item["actual_decision"], item["candidate_decision"]} == {"DENY", "AUTO"}
            for item in items
        ),
    }
    run_id = str(uuid4())
    await conn.execute(
        """INSERT INTO goal_policy_simulation_runs
           (id,tenant_id,project,candidate_policy_version,baseline_policy_version,run_mode,
            input_count,divergence_count,privilege_expansion_count,metrics,
            operational_counts_before,operational_counts_after,requested_by)
           VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5::uuid,$6,$7,$8,$9,$10::jsonb,$11::jsonb,$12::jsonb,$13::uuid)""",
        run_id, tenant_id, project, policy_id, baseline["id"] if baseline else None, run_mode,
        metrics["input_count"], metrics["divergence_count"], metrics["privilege_expansion_count"],
        json.dumps(metrics), json.dumps(before), json.dumps(after), actor.session_id,
    )
    for item in items:
        await conn.execute(
            """INSERT INTO goal_policy_simulation_items
               (tenant_id,run_id,source_decision_id,decision_input_hash,actual_decision,
                candidate_decision,divergence,privilege_expansion,masked_context,erased,masked,reason_codes)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11::jsonb,$12::text[])""",
            tenant_id, run_id, item["source_decision_id"], item["decision_input_hash"],
            item["actual_decision"], item["candidate_decision"], item["divergence"],
            item["privilege_expansion"], json.dumps(item["masked_context"]),
            json.dumps(item["erased"]), json.dumps(item["masked"]), item["reason_codes"],
        )
    return {"run_id": run_id, "candidate_policy_version": policy_id, "metrics": metrics,
            "operational_counts": {"before": before, "after": after}}


async def promote_policy(
    conn: Any, *, tenant_id: str, policy_id: str, replay_run_id: str,
    target_mode: str, reason: str, actor: ActorScope,
) -> dict[str, Any]:
    """Create an immutable canary/enabled revision only after zero expansion."""
    if actor.workspace_kind != "ceo_integrated":
        raise _error(403, "ceo_approval_required")
    source = await conn.fetchrow(
        "SELECT * FROM goal_approval_policy_versions WHERE tenant_id=$1::uuid AND id=$2::uuid FOR SHARE",
        tenant_id, policy_id,
    )
    if not source:
        raise _error(404, "policy_not_found")
    expected = {"audit_only": "canary", "canary": "enabled"}.get(str(source["mode"]))
    if target_mode != expected or (target_mode == "canary" and not source["project"]):
        raise _error(409, "invalid_policy_promotion")
    run = await conn.fetchrow(
        """SELECT * FROM goal_policy_simulation_runs
             WHERE tenant_id=$1::uuid AND id=$2::uuid AND candidate_policy_version=$3::uuid""",
        tenant_id, replay_run_id, policy_id,
    )
    if not run or int(run["input_count"]) < 1:
        raise _error(409, "insufficient_replay_evidence")
    if int(run["privilege_expansion_count"]) != 0:
        raise _error(409, "policy_privilege_expansion")
    result_id = str(uuid4())
    await conn.execute(
        """INSERT INTO goal_approval_policy_versions
           (id,tenant_id,project,policy,policy_hash,created_by,approved_by,mode,effective_at,
            previous_version_id,simulation_result)
           VALUES($1::uuid,$2::uuid,$3,$4::jsonb,$5,$6::uuid,$6::uuid,$7,clock_timestamp(),
                  $8::uuid,$9::jsonb)""",
        result_id, tenant_id, source["project"], json.dumps(_json(source["policy"])),
        source["policy_hash"], actor.session_id, target_mode, policy_id, json.dumps(_json(run["metrics"])),
    )
    await conn.execute(
        """INSERT INTO goal_policy_promotion_events
           (tenant_id,project,source_policy_version,resulting_policy_version,replay_run_id,
            event_type,from_mode,to_mode,rollback_policy_version,metrics,reason,approved_by)
           VALUES($1::uuid,$2,$3::uuid,$4::uuid,$5::uuid,'promote',$6,$7,$3::uuid,$8::jsonb,$9,$10::uuid)""",
        tenant_id, source["project"], policy_id, result_id, replay_run_id, source["mode"],
        target_mode, json.dumps(_json(run["metrics"])), reason, actor.session_id,
    )
    return {"policy_version": result_id, "mode": target_mode, "rollback_policy_version": policy_id}


async def rollback_policy(
    conn: Any, *, tenant_id: str, policy_id: str, rollback_policy_id: str,
    reason: str, actor: ActorScope,
) -> dict[str, Any]:
    """Create a new fail-closed revision and stale grants from the bad canary."""
    if actor.workspace_kind != "ceo_integrated":
        raise _error(403, "ceo_approval_required")
    current = await conn.fetchrow(
        "SELECT * FROM goal_approval_policy_versions WHERE tenant_id=$1::uuid AND id=$2::uuid FOR SHARE",
        tenant_id, policy_id,
    )
    rollback = await conn.fetchrow(
        "SELECT * FROM goal_approval_policy_versions WHERE tenant_id=$1::uuid AND id=$2::uuid FOR SHARE",
        tenant_id, rollback_policy_id,
    )
    if (not current or not rollback or current["project"] != rollback["project"]
            or current["mode"] not in {"canary", "enabled"}):
        raise _error(409, "invalid_policy_rollback")
    target_mode = str(rollback["mode"])
    if target_mode not in {"audit_only", "canary", "enabled"}:
        target_mode = "audit_only"
    result_id = str(uuid4())
    await conn.execute(
        """INSERT INTO goal_approval_policy_versions
           (id,tenant_id,project,policy,policy_hash,created_by,approved_by,mode,effective_at,
            previous_version_id,simulation_result)
           VALUES($1::uuid,$2::uuid,$3,$4::jsonb,$5,$6::uuid,$6::uuid,$7,clock_timestamp(),
                  $8::uuid,$9::jsonb)""",
        result_id, tenant_id, rollback["project"], json.dumps(_json(rollback["policy"])),
        rollback["policy_hash"], actor.session_id, target_mode, policy_id,
        json.dumps({"rollback_from": policy_id, "rollback_source": rollback_policy_id}),
    )
    await conn.execute(
        """UPDATE goal_auto_approval_grants SET status='stale',revoked_at=clock_timestamp(),
                  revoke_reason='policy_rollback'
            WHERE tenant_id=$1::uuid AND policy_version=$2::uuid AND status='active'""",
        tenant_id, policy_id,
    )
    await conn.execute(
        """INSERT INTO goal_policy_promotion_events
           (tenant_id,project,source_policy_version,resulting_policy_version,event_type,
            from_mode,to_mode,rollback_policy_version,metrics,reason,approved_by)
           VALUES($1::uuid,$2,$3::uuid,$4::uuid,'rollback',$5,$6,$7::uuid,$8::jsonb,$9,$10::uuid)""",
        tenant_id, current["project"], policy_id, result_id, current["mode"], target_mode,
        rollback_policy_id, json.dumps({"fail_closed": target_mode == "audit_only"}),
        reason, actor.session_id,
    )
    return {"policy_version": result_id, "mode": target_mode,
            "rolled_back_from": policy_id, "rollback_policy_version": rollback_policy_id}
