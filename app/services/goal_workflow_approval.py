"""M14 goal-workflow approvals, evidence gates, rollups, and bounded grants.

All mutating helpers expect the caller to own a database transaction.  Grant
selection and consumption deliberately happen in one locked statement path.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.core.goal_work_hierarchy_policy import (
    action_requires_mandatory_human,
    auto_approval_mode,
    mask_decision_context,
    workflow_approval_enabled,
)
from app.services.goal_work_hierarchy import ActorScope, _row_dict


def _error(status: int, code: str, message: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message or code})


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _require_enabled() -> None:
    if not workflow_approval_enabled():
        raise _error(404, "goal_workflow_approval_disabled")


async def _target(conn: Any, tenant_id: str, target_id: str) -> Any:
    row = await conn.fetchrow(
        """SELECT * FROM work_items WHERE id=$1::uuid AND tenant_id=$2::uuid FOR UPDATE""",
        target_id, tenant_id,
    )
    if not row:
        raise _error(404, "work_item_not_found")
    return row


async def append_event(
    conn: Any, *, tenant_id: str, project: str, aggregate_type: str,
    aggregate_id: str, event_type: str, actor: ActorScope | None,
    payload: Mapping[str, Any], correlation_id: str, causation_id: str | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO work_item_events
           (tenant_id,project,aggregate_type,aggregate_id,event_type,
            actor_session_id,actor_role_key,payload,correlation_id,causation_id)
           VALUES($1::uuid,$2,$3,$4::uuid,$5,$6::uuid,$7,$8::jsonb,$9::uuid,$10::uuid)""",
        tenant_id, project, aggregate_type, aggregate_id, event_type,
        actor.session_id if actor else None, actor.role_key if actor else None,
        json.dumps(mask_decision_context(dict(payload))), correlation_id, causation_id,
    )


async def create_change_set(
    conn: Any, *, tenant_id: str, actor: ActorScope, target_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    _require_enabled()
    item = await _target(conn, tenant_id, target_id)
    if not actor.may_access(str(item["project"])):
        raise _error(403, "project_scope_denied")
    patch = payload.get("patch")
    if not isinstance(patch, list):
        raise _error(422, "invalid_patch")
    if int(payload["base_version"]) != int(item["version"]):
        await conn.execute(
            """UPDATE work_item_change_sets SET state='superseded',updated_at=clock_timestamp()
               WHERE tenant_id=$1::uuid AND target_id=$2::uuid
                 AND state IN ('pending','approved')""", tenant_id, target_id,
        )
        raise _error(409, "version_conflict")
    patch_hash = canonical_hash(patch)
    existing = await conn.fetchrow(
        "SELECT * FROM work_item_change_sets WHERE tenant_id=$1::uuid AND idempotency_key=$2",
        tenant_id, payload["idempotency_key"],
    )
    if existing:
        if existing["patch_hash"] != patch_hash or str(existing["target_id"]) != target_id:
            raise _error(409, "idempotency_conflict")
        return _row_dict(existing)
    risk = "A3" if action_requires_mandatory_human(
        str(payload.get("action", "update")), str(payload.get("environment", "dev")),
        payload.get("risk_factors") or [],
    ) else str(payload.get("risk_tier") or "A2")
    row = await conn.fetchrow(
        """INSERT INTO work_item_change_sets
           (tenant_id,project,target_type,target_id,action,base_version,patch,patch_hash,
            rationale,expected_effect,rollback_plan,risk_tier,state,idempotency_key,requested_by)
           VALUES($1::uuid,$2,$3,$4::uuid,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,'pending',$13,$14::uuid)
           RETURNING *""",
        tenant_id, item["project"], item["type"], target_id, payload.get("action", "update"),
        payload["base_version"], json.dumps(patch), patch_hash, payload["rationale"],
        payload["expected_effect"], payload["rollback_plan"], risk,
        payload["idempotency_key"], actor.session_id,
    )
    correlation_id = str(uuid4())
    await append_event(
        conn, tenant_id=tenant_id, project=item["project"], aggregate_type="change_set",
        aggregate_id=str(row["id"]), event_type="change_set_submitted", actor=actor,
        payload={"patch_hash": patch_hash, "risk_tier": risk}, correlation_id=correlation_id,
    )
    return _row_dict(row)


async def route_change_set(conn: Any, *, tenant_id: str, change_set_id: str) -> dict[str, Any]:
    """Create exactly one legacy-compatible manual approval request."""
    row = await conn.fetchrow(
        """SELECT c.*, w.goal_id FROM work_item_change_sets c
           JOIN work_items w ON w.id=c.target_id AND w.tenant_id=c.tenant_id
           WHERE c.id=$1::uuid AND c.tenant_id=$2::uuid FOR UPDATE OF c""",
        change_set_id, tenant_id,
    )
    if not row:
        raise _error(404, "change_set_not_found")
    if row["approval_request_id"]:
        return {"change_set_id": change_set_id, "approval_request_id": str(row["approval_request_id"])}
    approval = await conn.fetchrow(
        """INSERT INTO agent_permission_requests
           (tenant_id,work_key,origin,action_type,action_summary,risk_level,decision,
            requested_by,approval_scope,max_executions,gate_source,tier)
           VALUES($1::uuid,$2,'goal_workflow',$3,$4,$5,'pending',$6,$7::jsonb,1,
                  'goal_workflow','approve') RETURNING id::text""",
        tenant_id, f"goal-workflow:{change_set_id}", row["action"],
        f"{row['target_type']} {row['target_id']}", str(row["risk_tier"]).lower(),
        str(row["requested_by"]), json.dumps({
            "change_set_id": change_set_id, "target_type": row["target_type"],
            "target_id": str(row["target_id"]), "base_version": row["base_version"],
            "patch_hash": row["patch_hash"], "project": row["project"],
            "required_role": "ceo" if row["risk_tier"] == "A3" else "project_lead",
            "bulk_allowed": row["risk_tier"] != "A3",
        }),
    )
    await conn.execute(
        "UPDATE work_item_change_sets SET approval_request_id=$1::uuid WHERE id=$2::uuid",
        approval["id"], change_set_id,
    )
    return {"change_set_id": change_set_id, "approval_request_id": approval["id"]}


async def decide_change_set(
    conn: Any, *, tenant_id: str, change_set_id: str, actor: ActorScope,
    approve: bool, reason: str, bulk: bool = False,
) -> dict[str, Any]:
    """Record a manual decision idempotently without executing inline."""
    row = await conn.fetchrow(
        "SELECT * FROM work_item_change_sets WHERE id=$1::uuid AND tenant_id=$2::uuid FOR UPDATE",
        change_set_id, tenant_id,
    )
    if not row:
        raise _error(404, "change_set_not_found")
    if bulk and row["risk_tier"] == "A3":
        raise _error(400, "bulk_not_allowed")
    if str(row["requested_by"]) == actor.session_id:
        raise _error(403, "self_approval_denied")
    desired = "approved" if approve else "rejected"
    if row["state"] == desired:
        return _row_dict(row)
    if row["state"] != "pending":
        raise _error(409, "approval_already_decided")
    updated = await conn.fetchrow(
        """UPDATE work_item_change_sets SET state=$2,decided_by=$3::uuid,
           decided_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=$1::uuid RETURNING *""",
        change_set_id, desired, actor.session_id,
    )
    await conn.execute(
        """UPDATE agent_permission_requests SET decision=$2,reason=$3,decided_by=$4,
           decided_at=clock_timestamp(),updated_at=clock_timestamp()
           WHERE id=$1::uuid AND decision='pending'""",
        row["approval_request_id"], desired, reason, actor.session_id,
    )
    await append_event(
        conn, tenant_id=tenant_id, project=row["project"], aggregate_type="change_set",
        aggregate_id=change_set_id, event_type=f"change_set_{desired}", actor=actor,
        payload={"reason": reason}, correlation_id=str(uuid4()),
    )
    return _row_dict(updated)


async def approval_preview(conn: Any, *, tenant_id: str, item_id: str) -> dict[str, Any]:
    item = await _target(conn, tenant_id, item_id)
    pending = await conn.fetchrow(
        """SELECT id::text,state,risk_tier,patch_hash,base_version,approval_request_id::text
           FROM work_item_change_sets WHERE tenant_id=$1::uuid AND target_id=$2::uuid
           ORDER BY created_at DESC LIMIT 1""", tenant_id, item_id,
    )
    return {"tenant_id": tenant_id, "project": item["project"], "version": item["version"],
            "last_error": None, "pending_approval_count": 1 if pending and pending["state"] == "pending" else 0,
            "approval": _row_dict(pending) if pending else None}


async def execute_change_set(
    conn: Any, *, tenant_id: str, change_set_id: str, execution_key: str,
    owner_instance: str, owner_epoch: int, actor: ActorScope,
) -> dict[str, Any]:
    """Validate immutable approval and enqueue one execution outbox event."""
    _require_enabled()
    row = await conn.fetchrow(
        """SELECT c.*,w.version AS target_version,w.project AS target_project
           FROM work_item_change_sets c JOIN work_items w ON w.id=c.target_id
            AND w.tenant_id=c.tenant_id
           WHERE c.id=$1::uuid AND c.tenant_id=$2::uuid FOR UPDATE OF c,w""",
        change_set_id, tenant_id,
    )
    if not row:
        raise _error(404, "change_set_not_found")
    if row["execution_key"]:
        if row["execution_key"] != execution_key:
            raise _error(409, "execution_key_conflict")
        return _row_dict(row)
    if row["state"] == "revoked":
        raise _error(409, "approval_revoked")
    if row["state"] != "approved":
        raise _error(409, "approval_required")
    if int(row["base_version"]) != int(row["target_version"]):
        await conn.execute("UPDATE work_item_change_sets SET state='superseded' WHERE id=$1::uuid", change_set_id)
        raise _error(409, "approval_superseded")
    if canonical_hash(row["patch"] if not isinstance(row["patch"], str) else json.loads(row["patch"])) != row["patch_hash"]:
        await append_event(
            conn, tenant_id=tenant_id, project=row["project"], aggregate_type="change_set",
            aggregate_id=change_set_id, event_type="patch_tamper_blocked", actor=actor,
            payload={"stored_patch_hash": row["patch_hash"]}, correlation_id=str(uuid4()),
        )
        raise _error(409, "patch_hash_mismatch")
    correlation_id = str(uuid4())
    await conn.execute(
        """UPDATE work_item_change_sets SET state='executing',execution_key=$1,
           owner_instance=$3,owner_epoch=$4,updated_at=clock_timestamp() WHERE id=$2::uuid""",
        execution_key, change_set_id, owner_instance, owner_epoch,
    )
    await conn.execute(
        """INSERT INTO goal_workflow_outbox
           (tenant_id,project,change_set_id,execution_key,event_type,payload,owner_instance,owner_epoch)
           VALUES($1::uuid,$2,$3::uuid,$4,'change_set.execute',$5::jsonb,$6,$7)
           ON CONFLICT (tenant_id,execution_key) DO NOTHING""",
        tenant_id, row["project"], change_set_id, execution_key,
        json.dumps({"owner_instance": owner_instance, "owner_epoch": owner_epoch,
                    "patch_hash": row["patch_hash"]}), owner_instance, owner_epoch,
    )
    await append_event(
        conn, tenant_id=tenant_id, project=row["project"], aggregate_type="change_set",
        aggregate_id=change_set_id, event_type="change_set_execution_requested", actor=actor,
        payload={"execution_key": execution_key, "owner_instance": owner_instance,
                 "owner_epoch": owner_epoch, "patch_hash": row["patch_hash"]},
        correlation_id=correlation_id,
    )
    return {"change_set_id": change_set_id, "state": "executing", "execution_key": execution_key}


def preview_grant(payload: Mapping[str, Any], *, actor_session_id: str) -> dict[str, Any]:
    actions = sorted({str(x) for x in payload.get("actions") or []})
    environments = sorted({str(x) for x in payload.get("environments") or []})
    rejected = [a for a in actions if action_requires_mandatory_human(a, "dev", [])]
    if "production" in environments:
        rejected.append("production")
    if str(payload.get("principal_session_id")) == actor_session_id:
        rejected.append("self_grant")
    return {
        "allowed": not rejected, "rejected_reasons": sorted(set(rejected)),
        "effective_scope": {**dict(payload), "actions": [a for a in actions if a not in rejected],
                            "environments": [e for e in environments if e != "production"]},
        "would_consume": False,
    }


async def create_grant(
    conn: Any, *, tenant_id: str, goal_id: str, actor: ActorScope,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    _require_enabled()
    preview = preview_grant(payload, actor_session_id=actor.session_id)
    if not preview["allowed"]:
        code = "self_grant_denied" if "self_grant" in preview["rejected_reasons"] else "mandatory_human"
        raise _error(403 if code == "self_grant_denied" else 422, code)
    requester = str(payload.get("requested_by") or actor.session_id)
    if str(payload["approved_by"]) in {str(payload["principal_session_id"]), requester}:
        raise _error(403, "self_approval_denied")
    goal = await conn.fetchrow("SELECT project FROM goals WHERE id=$1::uuid AND tenant_id=$2::uuid", goal_id, tenant_id)
    if not goal or not actor.may_access(goal["project"]):
        raise _error(404, "goal_not_found")
    assignment = await conn.fetchrow(
        """SELECT * FROM project_role_assignments WHERE id=$1::uuid AND tenant_id=$2::uuid
           AND project=$3 AND session_id=$4::uuid AND active""",
        payload["assignment_id"], tenant_id, goal["project"], payload["principal_session_id"],
    )
    if not assignment:
        raise _error(403, "local_assignment_required")
    scope = preview["effective_scope"]
    if payload.get("parent_grant_id"):
        parent = await conn.fetchrow(
            """SELECT * FROM goal_auto_approval_grants WHERE id=$1::uuid AND tenant_id=$2::uuid
               AND status='active' AND delegation_depth=1
               AND clock_timestamp() BETWEEN valid_from AND expires_at FOR UPDATE""",
            payload["parent_grant_id"], tenant_id,
        )
        if not parent:
            raise _error(403, "delegation_denied")
        within_parent = (
            set(scope["actions"]).issubset(set(parent["actions"]))
            and set(scope["environments"]).issubset(set(parent["environments"]))
            and int(payload.get("max_executions", 1)) <= int(parent["max_executions"]) - int(parent["used_executions"])
            and int(payload.get("max_files", 0)) <= int(parent["max_files"])
            and int(payload.get("max_rows", 0)) <= int(parent["max_rows"])
            and float(payload.get("max_cost_usd", 0)) <= float(parent["max_cost_usd"])
            and str(parent["project"]).upper() == str(goal["project"]).upper()
            and str(parent["goal_id"]) == str(goal_id)
        )
        if not within_parent:
            raise _error(403, "delegation_scope_exceeded")
    policy = await conn.fetchrow(
        """SELECT id::text,policy_hash FROM goal_approval_policy_versions
           WHERE tenant_id=$1::uuid AND (project=$2 OR project IS NULL)
             AND mode IN ('canary','enabled') ORDER BY (project=$2) DESC,effective_at DESC NULLS LAST LIMIT 1""",
        tenant_id, goal["project"],
    )
    if not policy:
        raise _error(409, "policy_unavailable")
    row = await conn.fetchrow(
        """INSERT INTO goal_auto_approval_grants
           (tenant_id,project,principal_session_id,assignment_id,goal_id,milestone_id,epic_id,story_id,
            actions,tool_groups,max_risk_tier,environments,conditions,max_executions,max_files,max_rows,
            max_cost_usd,max_parallel,max_duration_seconds,valid_from,expires_at,idle_timeout_seconds,
            delegation_depth,parent_grant_id,policy_version,scope_hash,revocation_strategy,requested_by,
            issued_by,approved_by)
           VALUES($1::uuid,$2,$3::uuid,$4::uuid,$5::uuid,$6::uuid,$7::uuid,$8::uuid,$9::text[],$10::text[],
            $11,$12::text[],$13::jsonb,$14,$15,$16,$17,$18,$19,$20::timestamptz,$21::timestamptz,$22,$23,
            $24::uuid,$25::uuid,$26,$27,$28::uuid,$29::uuid,$30::uuid) RETURNING *""",
        tenant_id, goal["project"], payload["principal_session_id"], payload["assignment_id"], goal_id,
        payload.get("milestone_id"), payload.get("epic_id"), payload.get("story_id"), scope["actions"],
        payload.get("tool_groups") or [], payload.get("max_risk_tier", "A1"), scope["environments"],
        json.dumps(payload.get("conditions") or {}), payload.get("max_executions", 1),
        payload.get("max_files", 0), payload.get("max_rows", 0), payload.get("max_cost_usd", 0),
        payload.get("max_parallel", 1), payload.get("max_duration_seconds", 0), payload["valid_from"],
        payload["expires_at"], payload.get("idle_timeout_seconds"), payload.get("delegation_depth", 0),
        payload.get("parent_grant_id"), policy["id"], canonical_hash(scope),
        payload.get("revocation_strategy", "cancel_now"), requester,
        actor.session_id, payload["approved_by"],
    )
    return _row_dict(row)


async def reserve_grant_use(
    conn: Any, *, tenant_id: str, actor: ActorScope, request: Mapping[str, Any], simulate: bool = False,
) -> dict[str, Any]:
    """Select one complete grant and atomically reserve all bounded budgets."""
    decision_id = str(uuid4())
    masked = mask_decision_context(dict(request))
    reason: list[str] = []
    if auto_approval_mode() == "off" or not workflow_approval_enabled():
        reason.append("auto_approval_disabled")
    if request.get("explicit_deny"):
        reason.append("explicit_deny")
    killed = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM goal_approval_kill_switches
           WHERE tenant_id=$1::uuid AND active
             AND (project IS NULL OR project=$2)
             AND (goal_id IS NULL OR goal_id=$3::uuid))""",
        tenant_id, request["project"], request["goal_id"],
    )
    if killed:
        reason.append("kill_switch")
    if action_requires_mandatory_human(
        str(request["action"]), str(request.get("environment", "dev")), request.get("risk_factors") or [],
    ):
        reason.append("mandatory_human")
    if reason:
        return await _decision(conn, tenant_id, decision_id, "CEO_APPROVAL", None, reason, masked, simulate)
    existing = await conn.fetchrow(
        "SELECT * FROM goal_auto_approval_uses WHERE tenant_id=$1::uuid AND execution_key=$2",
        tenant_id, request["execution_key"],
    )
    if existing:
        return {"decision_id": str(existing["decision_id"]), "decision": "AUTO",
                "matched_grant_id": str(existing["grant_id"]), "reason_codes": ["idempotent_replay"]}
    grant = await conn.fetchrow(
        """SELECT g.* FROM goal_auto_approval_grants g
           JOIN project_role_assignments a ON a.id=g.assignment_id AND a.tenant_id=g.tenant_id
           JOIN goal_approval_policy_versions p ON p.id=g.policy_version AND p.tenant_id=g.tenant_id
           WHERE g.tenant_id=$1::uuid AND g.project=$2 AND g.principal_session_id=$3::uuid
             AND g.goal_id=$4::uuid AND g.status='active' AND a.active
             AND p.mode IN ('canary','enabled') AND clock_timestamp() BETWEEN g.valid_from AND g.expires_at
             AND NOT EXISTS (SELECT 1 FROM goal_approval_policy_versions newer
                 WHERE newer.tenant_id=g.tenant_id AND (newer.project=g.project OR newer.project IS NULL)
                   AND newer.mode IN ('canary','enabled') AND newer.created_at>p.created_at)
             AND $5=ANY(g.actions) AND $6=ANY(g.environments)
             AND ($7::uuid IS NULL OR g.milestone_id IS NULL OR g.milestone_id=$7::uuid)
             AND ($8::uuid IS NULL OR g.epic_id IS NULL OR g.epic_id=$8::uuid)
             AND ($9::uuid IS NULL OR g.story_id IS NULL OR g.story_id=$9::uuid)
             AND g.max_files >= $10 AND g.max_rows >= $11 AND g.max_cost_usd >= $12
             AND g.max_duration_seconds >= $13
             AND g.used_executions < g.max_executions
             AND (SELECT count(*) FROM goal_auto_approval_uses u
                   WHERE u.tenant_id=g.tenant_id AND u.grant_id=g.id
                     AND u.status IN ('reserved','executing')) < g.max_parallel
             AND (g.parent_grant_id IS NULL OR EXISTS (
                   SELECT 1 FROM goal_auto_approval_grants pg
                    WHERE pg.id=g.parent_grant_id AND pg.tenant_id=g.tenant_id AND pg.status='active'
                      AND clock_timestamp() BETWEEN pg.valid_from AND pg.expires_at))
           ORDER BY cardinality(g.actions),g.expires_at FOR UPDATE OF g SKIP LOCKED LIMIT 1""",
        tenant_id, request["project"], actor.session_id, request["goal_id"], request["action"],
        request.get("environment", "dev"), request.get("milestone_id"), request.get("epic_id"),
        request.get("story_id"), request.get("estimated_files", 0), request.get("estimated_rows", 0),
        request.get("estimated_cost_usd", 0), request.get("estimated_duration_seconds", 0),
    )
    if not grant:
        return await _decision(conn, tenant_id, decision_id, "PROJECT_APPROVAL", None,
                               ["scope_or_budget_mismatch"], masked, simulate)
    if request.get("evidence_required") and not request.get("evidence_satisfied"):
        return await _decision(conn, tenant_id, decision_id, "PROJECT_APPROVAL", str(grant["id"]),
                               ["evidence_required"], masked, simulate)
    remaining = int(grant["max_executions"]) - int(grant["used_executions"])
    if simulate or auto_approval_mode() == "audit":
        return await _decision(conn, tenant_id, decision_id, "AUTO", str(grant["id"]),
                               ["simulation" if simulate else "audit_only"], masked, True,
                               remaining_uses=remaining)
    updated = await conn.fetchval(
        """UPDATE goal_auto_approval_grants SET used_executions=used_executions+1
           WHERE id=$1::uuid AND used_executions < max_executions RETURNING used_executions""", grant["id"],
    )
    if updated is None:
        return await _decision(conn, tenant_id, decision_id, "PROJECT_APPROVAL", None,
                               ["max_executions_exhausted"], masked, False)
    budget = {"files": request.get("estimated_files", 0), "rows": request.get("estimated_rows", 0),
              "cost_usd": request.get("estimated_cost_usd", 0),
              "duration_seconds": request.get("estimated_duration_seconds", 0)}
    await conn.execute(
        """INSERT INTO goal_auto_approval_uses
           (tenant_id,decision_id,execution_key,grant_id,grant_version,input_hash,target_type,target_id,
            target_version,patch_hash,action,budget_delta,status,correlation_id)
           VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5,$6,$7,$8::uuid,$9,$10,$11,$12::jsonb,'reserved',$13::uuid)""",
        tenant_id, decision_id, request["execution_key"], grant["id"], grant["grant_version"],
        canonical_hash(masked), request["target_type"], request["target_id"], request["target_version"],
        request.get("patch_hash"), request["action"], json.dumps(budget), request.get("correlation_id") or str(uuid4()),
    )
    return await _decision(conn, tenant_id, decision_id, "AUTO", str(grant["id"]),
                           ["grant_reserved"], masked, False, remaining_uses=int(grant["max_executions"])-int(updated))


async def _decision(conn: Any, tenant_id: str, decision_id: str, decision: str,
                    grant_id: str | None, reasons: Sequence[str], context: Mapping[str, Any],
                    simulate: bool, remaining_uses: int | None = None) -> dict[str, Any]:
    if not simulate:
        await conn.execute(
            """INSERT INTO goal_approval_decision_logs
               (id,tenant_id,decision,matched_grant_id,reason_codes,input_context,simulated)
               VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5::text[],$6::jsonb,FALSE)""",
            decision_id, tenant_id, decision, grant_id, list(reasons), json.dumps(context),
        )
    return {"decision_id": decision_id, "decision": decision, "matched_grant_id": grant_id,
            "reason_codes": list(reasons), "remaining_uses": remaining_uses, "simulated": simulate}


async def revoke_grant(conn: Any, *, tenant_id: str, grant_id: str, actor: ActorScope, reason: str) -> dict[str, Any]:
    _require_enabled()
    row = await conn.fetchrow(
        """UPDATE goal_auto_approval_grants SET status='revoked',revoked_by=$3::uuid,
           revoked_at=clock_timestamp(),revoke_reason=$4 WHERE id=$1::uuid AND tenant_id=$2::uuid
           AND status='active' RETURNING id::text,revocation_strategy""",
        grant_id, tenant_id, actor.session_id, reason,
    )
    if not row:
        existing = await conn.fetchrow(
            "SELECT id::text,status,revocation_strategy FROM goal_auto_approval_grants WHERE id=$1::uuid AND tenant_id=$2::uuid",
            grant_id, tenant_id,
        )
        if not existing:
            raise _error(404, "grant_not_found")
        return _row_dict(existing)
    if row["revocation_strategy"] == "cancel_now":
        await conn.execute(
            """UPDATE goal_auto_approval_uses SET status='failed',completed_at=clock_timestamp(),
               result=result||'{"revoked":"cancel_now"}'::jsonb
               WHERE tenant_id=$1::uuid AND grant_id=$2::uuid AND status IN ('reserved','executing')""",
            tenant_id, grant_id,
        )
    elif row["revocation_strategy"] == "compensate":
        await conn.execute(
            """UPDATE goal_auto_approval_uses SET status='manual_reconciliation',
               result=result||'{"revoked":"compensate","compensation_required":true}'::jsonb
               WHERE tenant_id=$1::uuid AND grant_id=$2::uuid AND status IN ('reserved','executing')""",
            tenant_id, grant_id,
        )
    # finish_current intentionally leaves existing rows executable; the grant
    # status prevents every new reservation.
    await conn.execute(
        """UPDATE goal_auto_approval_grants SET status='revoked',revoked_by=$2::uuid,
           revoked_at=clock_timestamp(),revoke_reason='parent_grant_revoked'
           WHERE parent_grant_id=$1::uuid AND status='active'""", grant_id, actor.session_id,
    )
    return _row_dict(row)


async def submit_review(conn: Any, *, tenant_id: str, item_id: str, actor: ActorScope) -> dict[str, Any]:
    await _target(conn, tenant_id, item_id)
    missing = await conn.fetchval(
        "SELECT NOT EXISTS(SELECT 1 FROM work_item_evidence WHERE work_item_id=$1::uuid AND tenant_id=$2::uuid)",
        item_id, tenant_id,
    )
    children_open = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM work_items WHERE parent_id=$1::uuid AND status<>'completed')", item_id,
    )
    if missing or children_open:
        raise _error(422, "evidence_required")
    row = await conn.fetchrow(
        "UPDATE work_items SET status='in_review',version=version+1,updated_at=clock_timestamp() WHERE id=$1::uuid RETURNING *",
        item_id,
    )
    return _row_dict(row)


async def review_item(conn: Any, *, tenant_id: str, item_id: str, actor: ActorScope,
                      accept: bool, reason: str = "") -> dict[str, Any]:
    item = await _target(conn, tenant_id, item_id)
    assignment_session = await conn.fetchval(
        "SELECT session_id::text FROM project_role_assignments WHERE id=$1::uuid", item["assignment_id"],
    ) if item["assignment_id"] else None
    if assignment_session == actor.session_id:
        raise _error(403, "separation_of_duties")
    status = "completed" if accept else "changes_requested"
    row = await conn.fetchrow(
        """UPDATE work_items SET status=$2,progress=CASE WHEN $2='completed' THEN 100 ELSE progress END,
           completed_at=CASE WHEN $2='completed' THEN clock_timestamp() ELSE NULL END,
           version=version+1,updated_at=clock_timestamp() WHERE id=$1::uuid RETURNING *""",
        item_id, status,
    )
    await rollup_ancestors(conn, tenant_id=tenant_id, item_id=item_id)
    return _row_dict(row)


async def rollup_ancestors(conn: Any, *, tenant_id: str, item_id: str) -> None:
    """Roll Task→Story→Epic→Milestone→Goal; never auto-accept an ancestor."""
    await conn.execute(
        """WITH RECURSIVE ancestors AS (
             SELECT parent_id FROM work_items WHERE id=$1::uuid AND tenant_id=$2::uuid
             UNION ALL SELECT w.parent_id FROM work_items w JOIN ancestors a ON w.id=a.parent_id
              WHERE w.parent_id IS NOT NULL
           ), rollup AS (
             SELECT p.id,COALESCE(round(100.0*count(*) FILTER(WHERE c.status='completed')/
                    NULLIF(count(*),0)),0)::int AS progress
             FROM work_items p JOIN ancestors a ON a.parent_id=p.id
             JOIN work_items c ON c.parent_id=p.id
             WHERE COALESCE((c.acceptance_criteria->>'optional')::boolean,FALSE)=FALSE GROUP BY p.id
           ) UPDATE work_items w SET progress=r.progress,
             status=CASE WHEN r.progress=100 AND w.status NOT IN ('completed','cancelled') THEN 'in_review' ELSE w.status END,
             updated_at=clock_timestamp() FROM rollup r WHERE w.id=r.id""",
        item_id, tenant_id,
    )
    # Legacy milestones have no progress column.  Move them to review only
    # after every Epic was independently accepted; final acceptance remains a
    # separate manual gate.  Goal progress is informational for the same reason.
    await conn.execute(
        """WITH scope AS (
             SELECT milestone_id,goal_id FROM work_items WHERE id=$1::uuid AND tenant_id=$2::uuid
           ), epic_state AS (
             SELECT s.milestone_id,s.goal_id,bool_and(w.status='completed') AS all_done
             FROM scope s JOIN work_items w ON w.milestone_id=s.milestone_id
              AND w.tenant_id=$2::uuid AND w.type='epic' GROUP BY s.milestone_id,s.goal_id
           ) UPDATE milestones m SET status=CASE WHEN e.all_done AND m.status<>'completed'
                    THEN 'in_review' ELSE m.status END,updated_at=clock_timestamp()
             FROM epic_state e WHERE m.id=e.milestone_id""",
        item_id, tenant_id,
    )
    await conn.execute(
        """WITH scope AS (SELECT goal_id FROM work_items WHERE id=$1::uuid AND tenant_id=$2::uuid),
           counts AS (SELECT w.goal_id,count(*) FILTER(WHERE w.status='completed') AS done,count(*) AS total
             FROM work_items w JOIN scope s ON s.goal_id=w.goal_id WHERE w.tenant_id=$2::uuid GROUP BY w.goal_id)
           UPDATE goals g SET progress=COALESCE(round(100.0*c.done/NULLIF(c.total,0)),0),
             updated_at=clock_timestamp() FROM counts c WHERE g.id=c.goal_id AND g.tenant_id=$2::uuid""",
        item_id, tenant_id,
    )
