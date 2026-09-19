"""M14 goal-workflow approvals, evidence gates, rollups, and bounded grants.

All mutating helpers expect the caller to own a database transaction.  Grant
selection and consumption deliberately happen in one locked statement path.
"""
from __future__ import annotations

import hashlib
import json
import os
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
from app.services.goal_policy_foundation import (
    ApplicationResult,
    SigningKey,
    validate_patch,
    verify_immediately_before_execution,
)
from app.services.goal_policy_foundation import patch_hash as canonical_patch_hash
from app.services.goal_work_hierarchy import ActorScope, _row_dict


def _error(status: int, code: str, message: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message or code})


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _change_set_body_hash(target_id: str, payload: Mapping[str, Any]) -> str:
    """Hash the complete semantic request body, not only its patch."""
    body = {key: value for key, value in payload.items() if key != "idempotency_key"}
    return canonical_hash({"target_id": target_id, "body": body})


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
    try:
        patch = validate_patch(patch)
    except (TypeError, ValueError) as exc:
        raise _error(422, "invalid_patch") from exc
    if int(payload["base_version"]) != int(item["version"]):
        await conn.execute(
            """UPDATE work_item_change_sets SET state='superseded',updated_at=clock_timestamp()
               WHERE tenant_id=$1::uuid AND target_id=$2::uuid
                 AND state IN ('pending','approved')""", tenant_id, target_id,
        )
        raise _error(409, "version_conflict")
    patch_hash = canonical_patch_hash(patch)
    body_hash = _change_set_body_hash(target_id, {**payload, "patch": patch})
    existing = await conn.fetchrow(
        "SELECT * FROM work_item_change_sets WHERE tenant_id=$1::uuid AND idempotency_key=$2",
        tenant_id, payload["idempotency_key"],
    )
    if existing:
        existing_body_hash = existing.get("body_hash")
        if (existing_body_hash or existing["patch_hash"]) != (body_hash if existing_body_hash else patch_hash):
            raise _error(409, "idempotency_conflict")
        return _row_dict(existing)
    risk = "A3" if action_requires_mandatory_human(
        str(payload.get("action", "update")), str(payload.get("environment", "dev")),
        payload.get("risk_factors") or [],
    ) else str(payload.get("risk_tier") or "A2")
    row = await conn.fetchrow(
        """INSERT INTO work_item_change_sets
           (tenant_id,project,target_type,target_id,action,base_version,target_version,patch,patch_hash,body_hash,risk_factors,
            rationale,expected_effect,rollback_plan,risk_tier,state,idempotency_key,requested_by,environment)
           VALUES($1::uuid,$2,$3,$4::uuid,$5,$6,$7,$8::jsonb,$9,$10,$11::text[],$12,$13,$14,$15,'pending',$16,$17::uuid,$18)
           RETURNING *""",
        tenant_id, item["project"], item["type"], target_id, payload.get("action", "update"),
        payload["base_version"], int(payload["base_version"]) + 1, json.dumps(patch), patch_hash, body_hash,
        list(payload.get("risk_factors") or []),
        payload["rationale"], payload["expected_effect"], payload["rollback_plan"], risk,
        payload["idempotency_key"], actor.session_id, payload.get("environment", "dev"),
    )
    correlation_id = str(uuid4())
    await append_event(
        conn, tenant_id=tenant_id, project=item["project"], aggregate_type="change_set",
        aggregate_id=str(row["id"]), event_type="change_set_submitted", actor=actor,
        payload={"patch_hash": patch_hash, "risk_tier": risk}, correlation_id=correlation_id,
    )
    return _row_dict(row)


async def route_change_set(
    conn: Any, *, tenant_id: str, change_set_id: str, actor: ActorScope,
) -> dict[str, Any]:
    """Create every required approval route without conflating execution."""
    row = await conn.fetchrow(
        """SELECT c.*, w.goal_id FROM work_item_change_sets c
           JOIN work_items w ON w.id=c.target_id AND w.tenant_id=c.tenant_id
           WHERE c.id=$1::uuid AND c.tenant_id=$2::uuid FOR UPDATE OF c""",
        change_set_id, tenant_id,
    )
    if not row:
        raise _error(404, "change_set_not_found")
    if not actor.may_access(str(row["project"])):
        raise _error(403, "project_scope_denied")
    routes = ["ceo", "independent_reviewer"] if row["risk_tier"] == "A3" else ["project_lead"]
    approvals: list[str] = []
    for route in routes:
        approval = await conn.fetchrow(
            """SELECT id::text FROM agent_permission_requests
               WHERE tenant_id=$1::uuid AND work_key=$2 AND gate_source='goal_workflow'
               ORDER BY created_at DESC LIMIT 1""",
            tenant_id, f"goal-workflow:{change_set_id}:{route}",
        )
        if not approval:
            approval = await conn.fetchrow(
        """INSERT INTO agent_permission_requests
           (tenant_id,work_key,origin,action_type,action_summary,risk_level,decision,
            requested_by,approval_scope,max_executions,gate_source,tier)
           VALUES($1::uuid,$2,'goal_workflow',$3,$4,$5,'pending',$6,$7::jsonb,1,
                  'goal_workflow','approve') RETURNING id::text""",
        tenant_id, f"goal-workflow:{change_set_id}:{route}", row["action"],
        f"{row['target_type']} {row['target_id']}", str(row["risk_tier"]).lower(),
        str(row["requested_by"]), json.dumps({
            "change_set_id": change_set_id, "target_type": row["target_type"],
            "target_id": str(row["target_id"]), "base_version": row["base_version"],
            "patch_hash": row["patch_hash"], "project": row["project"],
            "required_role": route,
            "bulk_allowed": row["risk_tier"] != "A3",
        }),
            )
        approvals.append(str(approval["id"]))
        await conn.execute(
            """INSERT INTO work_item_change_set_approval_routes
               (tenant_id,project,change_set_id,route_key,required_role,approval_request_id)
               VALUES($1::uuid,$2,$3::uuid,$4,$4,$5::uuid)
               ON CONFLICT (tenant_id,change_set_id,route_key) DO NOTHING""",
            tenant_id, row["project"], change_set_id, route, approval["id"],
        )
    await conn.execute(
        "UPDATE work_item_change_sets SET approval_request_id=$1::uuid WHERE id=$2::uuid",
        approvals[0], change_set_id,
    )
    return {"change_set_id": change_set_id, "approval_request_id": approvals[0],
            "approval_request_ids": approvals, "required_routes": routes}


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
    if row["state"] in {"rejected", desired}:
        return _row_dict(row)
    if row["state"] not in {"pending", "approved"}:
        raise _error(409, "approval_already_decided")
    route = await conn.fetchrow(
        """SELECT r.* FROM work_item_change_set_approval_routes r
            WHERE r.tenant_id=$1::uuid AND r.change_set_id=$2::uuid
              AND r.state='pending'
              AND NOT EXISTS (SELECT 1 FROM work_item_change_set_approval_decisions d
                 WHERE d.tenant_id=r.tenant_id AND d.change_set_id=r.change_set_id
                   AND d.actor_session_id=$3::uuid)
            ORDER BY CASE r.required_role WHEN 'ceo' THEN 1 WHEN 'project_lead' THEN 2 ELSE 3 END
            FOR UPDATE LIMIT 1""",
        tenant_id, change_set_id, actor.session_id,
    )
    # Legacy/fake adapters may return the change-set row for an unrecognised
    # fetchrow call.  Only a row carrying the explicit route contract may be
    # treated as a multi-approval route.
    if route and not route.get("required_role"):
        route = None
    # Compatibility for databases being migrated: the legacy single route is
    # treated exactly as before. New rows always have an explicit route.
    route_obj = _row_dict(route) if route and "required_role" in route else None
    if route_obj is None and route:
        route = None
    if route is None:
        has_routes = bool(await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM work_item_change_set_approval_routes
                 WHERE tenant_id=$1::uuid AND change_set_id=$2::uuid)""",
            tenant_id, change_set_id,
        ))
        if has_routes:
            raise _error(403, "approval_route_not_eligible")
    required_role = str(route_obj["required_role"]) if route_obj else (
        "ceo" if row["risk_tier"] == "A3" else "project_lead"
    )
    if required_role == "ceo":
        if actor.workspace_kind not in {"ceo", "ceo_integrated"} or actor.role_key.strip().lower() != "ceo":
            raise _error(403, "ceo_approval_required")
    elif required_role == "project_lead":
        lead = await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM project_role_assignments
                 WHERE tenant_id=$1::uuid AND project=$2 AND session_id=$3::uuid AND active
                   AND lower(role_key) IN ('project_lead','project lead'))""",
            tenant_id, row["project"], actor.session_id,
        )
        if not lead:
            raise _error(403, "project_lead_approval_required")
    elif required_role == "independent_reviewer" and actor.role_key.strip().lower() not in {
        "independent_reviewer", "independent reviewer", "reviewer", "qa",
    }:
        raise _error(403, "independent_review_required")
    if route:
        await conn.execute(
            """INSERT INTO work_item_change_set_approval_decisions
               (tenant_id,project,change_set_id,route_id,actor_session_id,decision,reason)
               VALUES($1::uuid,$2,$3::uuid,$4::uuid,$5::uuid,$6,$7)
               ON CONFLICT (tenant_id,route_id,actor_session_id) DO NOTHING""",
            tenant_id, row["project"], change_set_id, route["id"], actor.session_id, desired, reason,
        )
        await conn.execute(
            """UPDATE work_item_change_set_approval_routes SET state=$2,decided_at=clock_timestamp()
                WHERE id=$1::uuid AND state='pending'""", route["id"], desired,
        )
    any_rejection = not approve or bool(await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM work_item_change_set_approval_routes
             WHERE tenant_id=$1::uuid AND change_set_id=$2::uuid AND state='rejected')""",
        tenant_id, change_set_id,
    ))
    all_approved = bool(await conn.fetchval(
        """SELECT NOT EXISTS(SELECT 1 FROM work_item_change_set_approval_routes
             WHERE tenant_id=$1::uuid AND change_set_id=$2::uuid AND state<>'approved')""",
        tenant_id, change_set_id,
    )) if route else approve
    aggregate_state = "rejected" if any_rejection else ("approved" if all_approved else "pending")
    updated = await conn.fetchrow(
        """UPDATE work_item_change_sets SET state=$2,decided_by=$3::uuid,
           decided_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=$1::uuid RETURNING *""",
        change_set_id, aggregate_state, actor.session_id,
    )
    await conn.execute(
        """UPDATE agent_permission_requests SET decision=$2,reason=$3,decided_by=$4,
           decided_at=clock_timestamp(),updated_at=clock_timestamp()
           WHERE id=$1::uuid AND decision='pending'""",
        route["approval_request_id"] if route else row["approval_request_id"], desired, reason, actor.session_id,
    )
    await append_event(
        conn, tenant_id=tenant_id, project=row["project"], aggregate_type="change_set",
        aggregate_id=change_set_id, event_type=f"change_set_route_{desired}", actor=actor,
        payload={"reason": reason, "route": required_role, "aggregate_state": aggregate_state},
        correlation_id=str(uuid4()),
    )
    return _row_dict(updated)


async def approval_preview(conn: Any, *, tenant_id: str, item_id: str, actor: ActorScope) -> dict[str, Any]:
    item = await _target(conn, tenant_id, item_id)
    if not actor.may_access(str(item["project"])):
        raise _error(403, "project_scope_denied")
    pending = await conn.fetchrow(
        """SELECT id::text,state,risk_tier,patch,patch_hash,base_version,
                  target_version,action,rationale,expected_effect,rollback_plan,
                  environment,risk_factors,created_at,last_error,
                  approval_request_id::text
           FROM work_item_change_sets WHERE tenant_id=$1::uuid AND target_id=$2::uuid
           ORDER BY created_at DESC LIMIT 1""", tenant_id, item_id,
    )
    current = {
        key: item.get(key)
        for key in ("title", "description", "status", "priority", "progress", "version")
    }
    return {"tenant_id": tenant_id, "project": item["project"], "version": item["version"],
            "last_error": pending.get("last_error") if pending else None,
            "pending_approval_count": 1 if pending and pending["state"] == "pending" else 0,
            "current": current, "approval": _row_dict(pending) if pending else None}


async def request_outbox_retry(
    conn: Any, *, tenant_id: str, item_id: str, actor: ActorScope, reason: str,
) -> dict[str, Any]:
    """Move one known-failed delivery back to pending without replaying unknown outcomes."""
    _require_enabled()
    item = await _target(conn, tenant_id, item_id)
    project = str(item["project"])
    if not actor.may_access(project):
        raise _error(403, "project_scope_denied")
    row = await conn.fetchrow(
        """SELECT o.id::text,o.change_set_id::text,o.execution_key,o.attempts,
                  c.requested_by::text
             FROM goal_workflow_outbox o
             JOIN work_item_change_sets c
               ON c.id=o.change_set_id AND c.tenant_id=o.tenant_id
            WHERE o.tenant_id=$1::uuid AND o.project=$2 AND c.target_id=$3::uuid
              AND o.status='failed'
            ORDER BY o.created_at DESC,o.id DESC LIMIT 1 FOR UPDATE OF o""",
        tenant_id, project, item_id,
    )
    if not row:
        raise _error(409, "failed_delivery_not_found")
    if actor.workspace_kind != "ceo_integrated" and str(row["requested_by"]) != actor.session_id:
        lead = await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM project_role_assignments
                 WHERE tenant_id=$1::uuid AND project=$2 AND session_id=$3::uuid
                   AND active AND lower(role_key) IN ('project_lead','project lead'))""",
            tenant_id, project, actor.session_id,
        )
        if not lead:
            raise _error(403, "retry_not_authorized")
    updated = await conn.fetchrow(
        """UPDATE goal_workflow_outbox
              SET status='pending',available_at=clock_timestamp(),last_error=NULL,
                  claim_token=NULL,claimed_at=NULL,publish_state='pending'
            WHERE id=$1::uuid AND tenant_id=$2::uuid AND status='failed'
            RETURNING id::text,change_set_id::text,execution_key,status,attempts,available_at""",
        row["id"], tenant_id,
    )
    if not updated:
        raise _error(409, "delivery_state_changed")
    await append_event(
        conn, tenant_id=tenant_id, project=project, aggregate_type="change_set",
        aggregate_id=str(row["change_set_id"]), event_type="outbox_retry_requested",
        actor=actor, payload={"reason": reason, "attempts": row["attempts"]},
        correlation_id=str(uuid4()),
    )
    return {**_row_dict(updated), "state": "retry_pending"}


_MUTABLE_WORK_ITEM_FIELDS = {
    "/title": "title", "/description": "description", "/status": "status",
    "/priority": "priority", "/progress": "progress",
}


async def _apply_internal_patch(
    conn: Any, *, tenant_id: str, row: Mapping[str, Any], patch: Sequence[Mapping[str, Any]],
) -> None:
    """Apply the ordered patch to a bounded work-item surface in one UPDATE."""
    current = await conn.fetchrow(
        """SELECT title,description,status,priority,progress,version
             FROM work_items WHERE id=$1::uuid AND tenant_id=$2::uuid FOR UPDATE""",
        row["target_id"], tenant_id,
    )
    if not current or int(current["version"]) != int(row["base_version"]):
        raise _error(409, "target_version_changed")
    state = {field: current[field] for field in _MUTABLE_WORK_ITEM_FIELDS.values()}
    changed_fields: set[str] = set()
    for operation in patch:
        field = _MUTABLE_WORK_ITEM_FIELDS.get(str(operation.get("path")))
        op = str(operation["op"])
        if field is None:
            raise _error(422, "unsupported_patch_path")
        if op == "test":
            if state[field] != operation.get("value"):
                raise _error(409, "patch_test_failed")
            continue
        if op in {"copy", "move"}:
            source = _MUTABLE_WORK_ITEM_FIELDS.get(str(operation.get("from")))
            if source is None:
                raise _error(422, "unsupported_patch_path")
            state[field] = state[source]
            changed_fields.add(field)
            if op == "move" and source != field:
                if source != "description":
                    raise _error(422, "unsupported_patch_remove")
                state[source] = None
                changed_fields.add(source)
            continue
        if op == "remove":
            if field != "description":
                raise _error(422, "unsupported_patch_remove")
            state[field] = None
            changed_fields.add(field)
            continue
        if op not in {"add", "replace"}:
            raise _error(422, "unsupported_patch_operation")
        state[field] = operation.get("value")
        changed_fields.add(field)
    if not changed_fields:
        raise _error(422, "patch_has_no_mutation")
    ordered_fields = sorted(changed_fields)
    values = [state[field] for field in ordered_fields]
    # Patch values begin at $5; identity/version arguments occupy $1..$4.
    field_updates = ",".join(f"{field}=${index + 5}" for index, field in enumerate(ordered_fields))
    completion_update = (
        ",completed_at=CASE WHEN status='completed' THEN COALESCE(completed_at,clock_timestamp()) "
        "ELSE completed_at END" if "status" in changed_fields else ""
    )
    sql = (
        "UPDATE work_items SET " + (field_updates + "," if field_updates else "") +
        "version=$3,updated_at=clock_timestamp()" + completion_update + " "
        "WHERE id=$1::uuid AND tenant_id=$2::uuid AND version=$4 RETURNING id"
    )
    changed = await conn.fetchrow(
        sql, row["target_id"], tenant_id, row["target_version"], row["base_version"], *values,
    )
    if not changed:
        raise _error(409, "target_version_changed")


async def execute_change_set(
    conn: Any, *, tenant_id: str, change_set_id: str, execution_key: str,
    owner_instance: str, owner_epoch: int, actor: ActorScope,
) -> dict[str, Any]:
    """Validate immutable approval and enqueue one execution outbox event."""
    _require_enabled()
    row = await conn.fetchrow(
        """SELECT c.*,w.version AS current_target_version,w.project AS target_project,w.goal_id AS target_goal_id
           FROM work_item_change_sets c JOIN work_items w ON w.id=c.target_id
            AND w.tenant_id=c.tenant_id
           WHERE c.id=$1::uuid AND c.tenant_id=$2::uuid FOR UPDATE OF c,w""",
        change_set_id, tenant_id,
    )
    if not row:
        raise _error(404, "change_set_not_found")
    if not actor.may_access(str(row["project"])) or str(row["target_project"]).upper() != str(row["project"]).upper():
        raise _error(403, "project_scope_denied")
    lease_owner = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM chat_turn_executions
             WHERE session_id=$1::uuid AND owner_instance=$2 AND owner_epoch=$3
               AND status IN ('running','retrying') AND lease_expires_at > clock_timestamp())""",
        actor.session_id, owner_instance, owner_epoch,
    )
    if not lease_owner:
        raise _error(409, "execution_owner_lease_invalid")
    if row["execution_key"]:
        if row["execution_key"] != execution_key:
            raise _error(409, "execution_key_conflict")
        return _row_dict(row)
    if row["state"] in {"revoked", "expired", "superseded"}:
        raise _error(409, "approval_revoked")
    if row["state"] != "approved":
        raise _error(409, "approval_required")
    if int(row["base_version"]) != int(row["current_target_version"]):
        await conn.execute("UPDATE work_item_change_sets SET state='superseded' WHERE id=$1::uuid", change_set_id)
        raise _error(409, "approval_superseded")
    patch = row["patch"] if not isinstance(row["patch"], str) else json.loads(row["patch"])
    if canonical_patch_hash(patch) != row["patch_hash"]:
        await append_event(
            conn, tenant_id=tenant_id, project=row["project"], aggregate_type="change_set",
            aggregate_id=change_set_id, event_type="patch_tamper_blocked", actor=actor,
            payload={"stored_patch_hash": row["patch_hash"]}, correlation_id=str(uuid4()),
        )
        raise _error(409, "patch_hash_mismatch")
    decision = await conn.fetchrow(
        """SELECT d.* FROM goal_policy_decisions d
            WHERE d.tenant_id=$1::uuid AND d.project=$2 AND d.target_id=$3::uuid
              AND d.target_version=$4 AND d.patch_hash=$5
              AND d.principal_session_id=$6::uuid
              AND d.effective_application_result IN ('AUTO','APPROVAL_REQUIRED')
            ORDER BY d.decided_at DESC LIMIT 1""",
        tenant_id, row["project"], row["target_id"], row["base_version"], row["patch_hash"],
        row["requested_by"],
    )
    secret = os.getenv("GOAL_POLICY_DECISION_SIGNING_SECRET", "").encode()
    if not decision or len(secret) < 32:
        raise _error(409, "policy_decision_unverifiable")
    envelope = _row_dict(decision)
    for name in (
        "id", "tenant_id", "principal_session_id", "assignment_id", "target_id",
        "policy_version", "matched_grant_id",
    ):
        if envelope.get(name) is not None:
            envelope[name] = str(envelope[name])
    envelope["decision_id"] = envelope.pop("id")
    decided_at = envelope.get("decided_at")
    if hasattr(decided_at, "isoformat"):
        envelope["decided_at"] = decided_at.isoformat().replace("+00:00", "Z")
    elif str(decided_at).endswith("+00:00"):
        envelope["decided_at"] = str(decided_at)[:-6] + "Z"
    expected_input = {
        "tenant_id": tenant_id, "project": row["project"],
        "workspace_kind": envelope["workspace_kind"],
        "principal_session_id": envelope["principal_session_id"],
        "assignment_id": envelope["assignment_id"], "target_type": row["target_type"],
        "target_id": str(row["target_id"]), "action": row["action"],
        "base_version": row["base_version"], "patch_hash": row["patch_hash"],
        "environment": row["environment"],
        "risk_factors": list(row.get("risk_factors") or []),
        "precondition_snapshot_hash": envelope["precondition_snapshot_hash"],
        "policy_version": str(envelope["policy_version"]),
        "grant_id": str(envelope["matched_grant_id"]) if envelope.get("matched_grant_id") else None,
        "grant_version": envelope.get("grant_version"),
    }
    verified, failure = await verify_immediately_before_execution(
        conn, envelope,
        signing_key=SigningKey(str(envelope["signature_key_id"]), int(envelope["signature_key_version"]), secret),
        expected_input=expected_input,
        accepted_results=(ApplicationResult.AUTO, ApplicationResult.APPROVAL_REQUIRED),
    )
    if not verified:
        raise _error(409, failure or "policy_revalidation_failed")
    reservation = await conn.fetchrow(
        """SELECT g.status,g.project,g.goal_id::text,
                  clock_timestamp() BETWEEN g.valid_from AND g.expires_at AS valid_now
             FROM goal_auto_approval_uses u
             JOIN goal_auto_approval_grants g ON g.id=u.grant_id AND g.tenant_id=u.tenant_id
            WHERE u.tenant_id=$1::uuid AND u.target_id=$2::uuid AND u.target_version=$3
              AND u.patch_hash IS NOT DISTINCT FROM $4 AND u.action=$5
              AND u.status IN ('reserved','executing')
            ORDER BY u.reserved_at DESC LIMIT 1 FOR KEY SHARE OF g""",
        tenant_id, row["target_id"], row["base_version"], row["patch_hash"], row["action"],
    )
    if reservation and (reservation["status"] != "active" or not reservation["valid_now"]
                        or str(reservation["project"]).upper() != str(row["target_project"]).upper()
                        or str(reservation["goal_id"]) != str(row["target_goal_id"])):
        raise _error(409, "grant_revalidation_failed")
    correlation_id = str(uuid4())
    await _apply_internal_patch(conn, tenant_id=tenant_id, row=row, patch=patch)
    await conn.execute(
        """INSERT INTO goal_workflow_effects
           (tenant_id,project,execution_key,change_set_id,owner_instance,owner_epoch,effect_kind,state,applied_at)
           VALUES($1::uuid,$2,$3,$4::uuid,$5,$6,'internal','applied',clock_timestamp())
           ON CONFLICT (tenant_id,execution_key) DO NOTHING""",
        tenant_id, row["project"], execution_key, change_set_id, owner_instance, owner_epoch,
    )
    await conn.execute(
        """UPDATE work_item_change_sets SET state='executed',execution_key=$1,
           owner_instance=$3,owner_epoch=$4,executed_at=clock_timestamp(),updated_at=clock_timestamp()
           WHERE id=$2::uuid""",
        execution_key, change_set_id, owner_instance, owner_epoch,
    )
    await conn.execute(
        """INSERT INTO goal_workflow_outbox
           (tenant_id,project,change_set_id,execution_key,event_type,payload,owner_instance,owner_epoch,decision_id)
           VALUES($1::uuid,$2,$3::uuid,$4,'change_set.execute',$5::jsonb,$6,$7,$8::uuid)
           ON CONFLICT (tenant_id,execution_key) DO NOTHING""",
        tenant_id, row["project"], change_set_id, execution_key,
        json.dumps({"owner_instance": owner_instance, "owner_epoch": owner_epoch,
                    "patch_hash": row["patch_hash"]}), owner_instance, owner_epoch, decision["id"],
    )
    await append_event(
        conn, tenant_id=tenant_id, project=row["project"], aggregate_type="change_set",
        aggregate_id=change_set_id, event_type="change_set_execution_requested", actor=actor,
        payload={"execution_key": execution_key, "owner_instance": owner_instance,
                 "owner_epoch": owner_epoch, "patch_hash": row["patch_hash"]},
        correlation_id=correlation_id,
    )
    return {"change_set_id": change_set_id, "state": "executed", "execution_key": execution_key}


async def claim_outbox(
    conn: Any, *, owner_instance: str, owner_epoch: int, limit: int = 20,
) -> list[dict[str, Any]]:
    """Claim publish work; this is the only W-14a path that uses SKIP LOCKED."""
    rows = await conn.fetch(
        """WITH candidates AS (
             SELECT o.id FROM goal_workflow_outbox o
              JOIN work_item_change_sets c ON c.id=o.change_set_id AND c.tenant_id=o.tenant_id
              JOIN chat_turn_executions e ON e.session_id=c.requested_by
               AND e.owner_instance=$1 AND e.owner_epoch=$2
               AND e.status IN ('running','retrying') AND e.lease_expires_at>clock_timestamp()
              WHERE o.status IN ('pending','failed') AND o.available_at<=clock_timestamp()
              ORDER BY o.available_at,o.created_at FOR UPDATE OF o SKIP LOCKED LIMIT $3
           )
           UPDATE goal_workflow_outbox o SET status='delivering',attempts=attempts+1,
                  owner_instance=$1,owner_epoch=$2
             FROM candidates c WHERE o.id=c.id RETURNING o.*""",
        owner_instance, owner_epoch, limit,
    )
    return [_row_dict(row) for row in rows]


async def complete_outbox_delivery(
    conn: Any, *, tenant_id: str, outbox_id: str, owner_instance: str, owner_epoch: int,
    outcome: str, error: str | None = None,
) -> dict[str, Any]:
    """Fence acknowledgements; unknown external outcomes require reconciliation."""
    if outcome not in {"delivered", "failed", "unknown"}:
        raise _error(422, "invalid_delivery_outcome")
    status = "reconciliation_required" if outcome == "unknown" else outcome
    row = await conn.fetchrow(
        """UPDATE goal_workflow_outbox SET status=$5,
               delivered_at=CASE WHEN $5='delivered' THEN clock_timestamp() ELSE delivered_at END,
               last_error=$6,
               publish_state=CASE WHEN $5='reconciliation_required'
                    THEN 'reconciliation_required' ELSE publish_state END
             WHERE id=$1::uuid AND tenant_id=$2::uuid AND owner_instance=$3 AND owner_epoch=$4
               AND status='delivering' RETURNING *""",
        outbox_id, tenant_id, owner_instance, owner_epoch, status, error,
    )
    if not row:
        raise _error(409, "outbox_owner_fence_lost")
    if outcome == "unknown":
        await conn.execute(
            """UPDATE goal_workflow_effects SET state='reconciliation_required',
                   result=result||jsonb_build_object('unknown_external_outcome',true,'error',$4::text)
                 WHERE tenant_id=$1::uuid AND execution_key=$2
                   AND owner_instance=$3 AND owner_epoch=$5""",
            tenant_id, row["execution_key"], owner_instance, error, owner_epoch,
        )
    return _row_dict(row)


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
    requester = actor.session_id
    goal = await conn.fetchrow("SELECT project FROM goals WHERE id=$1::uuid AND tenant_id=$2::uuid", goal_id, tenant_id)
    if not goal or not actor.may_access(goal["project"]):
        raise _error(404, "goal_not_found")
    if actor.workspace_kind != "ceo_integrated":
        issuer_is_lead = await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM project_role_assignments
                 WHERE tenant_id=$1::uuid AND project=$2 AND session_id=$3::uuid AND active
                   AND lower(role_key) IN ('project_lead','project lead'))""",
            tenant_id, goal["project"], actor.session_id,
        )
        if not issuer_is_lead:
            raise _error(403, "project_lead_approval_required")
    approval = await conn.fetchrow(
        """SELECT decided_by::text,requested_by::text,approval_scope FROM agent_permission_requests
             WHERE id=$1::uuid AND tenant_id=$2::uuid AND gate_source='goal_workflow'
               AND decision='approved' FOR UPDATE""",
        payload["approval_request_id"], tenant_id,
    )
    if not approval:
        raise _error(409, "independent_approval_required")
    scope_data = approval["approval_scope"]
    if isinstance(scope_data, str):
        scope_data = json.loads(scope_data)
    approved_by = str(approval["decided_by"] or "")
    if (not approved_by or approved_by in {requester, str(payload["principal_session_id"])}
            or str(scope_data.get("project", "")).upper() != str(goal["project"]).upper()):
        raise _error(403, "independent_approval_required")
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
        if parent.get("parent_grant_id") or int(payload.get("delegation_depth", 0)) != 0:
            raise _error(422, "delegation_depth_exceeded")
        if str(parent["principal_session_id"]) != actor.session_id:
            raise _error(403, "delegation_issuer_mismatch")
        if str(parent["issued_by"]) == str(payload["principal_session_id"]):
            raise _error(403, "separation_of_duties")
        within_parent = (
            set(scope["actions"]).issubset(set(parent["actions"]))
            and set(scope["environments"]).issubset(set(parent["environments"]))
            and int(payload.get("max_executions", 1)) <= int(parent["max_executions"]) - int(parent["used_executions"])
            and int(payload.get("max_files", 0)) <= int(parent["max_files"])
            and int(payload.get("max_rows", 0)) <= int(parent["max_rows"])
            and float(payload.get("max_cost_usd", 0)) <= float(parent["max_cost_usd"])
            and int(payload.get("max_parallel", 1)) <= int(parent["max_parallel"])
            and int(payload.get("max_duration_seconds", 0)) <= int(parent["max_duration_seconds"])
            and set(payload.get("tool_groups") or []).issubset(set(parent["tool_groups"]))
            and str(payload.get("max_risk_tier", "A1")) <= str(parent["max_risk_tier"])
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
        actor.session_id, approved_by,
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
    target = await conn.fetchrow(
        """SELECT project,goal_id::text,version FROM work_items
             WHERE id=$1::uuid AND tenant_id=$2::uuid""",
        request["target_id"], tenant_id,
    )
    if (not target or str(target["project"]).upper() != str(request["project"]).upper()
            or str(target["goal_id"]) != str(request["goal_id"])
            or int(target["version"]) != int(request["target_version"])
            or not actor.may_access(str(target["project"]))):
        return await _decision(conn, tenant_id, decision_id, "PROJECT_APPROVAL", None,
                               ["target_scope_or_version_mismatch"], masked, simulate)
    # Hash the full canonical request before masking; only the digest is stored.
    # Masked payloads can collide when two different secrets become [REDACTED].
    request_hash = canonical_hash(dict(request))
    existing = await conn.fetchrow(
        "SELECT * FROM goal_auto_approval_uses WHERE tenant_id=$1::uuid AND execution_key=$2",
        tenant_id, request["execution_key"],
    )
    if existing:
        if str(existing["input_hash"]) != request_hash:
            raise _error(409, "execution_key_conflict")
        if existing["status"] == "manual_reconciliation":
            return {"decision_id": str(existing["decision_id"]), "decision": "PROJECT_APPROVAL",
                    "matched_grant_id": str(existing["grant_id"]),
                    "reason_codes": ["reconciliation_required"], "auto_replay": False}
        return {"decision_id": str(existing["decision_id"]), "decision": "AUTO",
                "matched_grant_id": str(existing["grant_id"]), "reason_codes": ["idempotent_replay"]}
    grant = await conn.fetchrow(
        """WITH RECURSIVE ancestors AS (
               SELECT child.id AS leaf_id,parent.* FROM goal_auto_approval_grants child
               JOIN goal_auto_approval_grants parent ON parent.id=child.parent_grant_id
                AND parent.tenant_id=child.tenant_id WHERE child.tenant_id=$1::uuid
               UNION ALL
               SELECT a.leaf_id,parent.* FROM ancestors a
               JOIN goal_auto_approval_grants parent ON parent.id=a.parent_grant_id
                AND parent.tenant_id=a.tenant_id
           ) SELECT g.* FROM goal_auto_approval_grants g
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
             AND g.used_executions < g.max_executions
             AND (SELECT count(*) FROM goal_auto_approval_uses u
                   WHERE u.tenant_id=g.tenant_id AND u.grant_id=g.id
                     AND u.status IN ('reserved','executing')) < g.max_parallel
             AND (g.parent_grant_id IS NULL OR EXISTS (
                   SELECT 1 FROM goal_auto_approval_grants pg
                    WHERE pg.id=g.parent_grant_id AND pg.tenant_id=g.tenant_id AND pg.status='active'
                      AND clock_timestamp() BETWEEN pg.valid_from AND pg.expires_at))
             AND NOT EXISTS (SELECT 1 FROM ancestors x WHERE x.leaf_id=g.id AND
                    (x.status<>'active' OR clock_timestamp() NOT BETWEEN x.valid_from AND x.expires_at
                     OR x.project<>g.project OR x.goal_id<>g.goal_id
                     OR NOT g.actions <@ x.actions OR NOT g.environments <@ x.environments
                     OR x.delegation_depth<>1))
           ORDER BY cardinality(g.actions),g.expires_at FOR UPDATE OF g LIMIT 1""",
        tenant_id, request["project"], actor.session_id, request["goal_id"], request["action"],
        request.get("environment", "dev"), request.get("milestone_id"), request.get("epic_id"),
        request.get("story_id"),
    )
    if not grant:
        return await _decision(conn, tenant_id, decision_id, "PROJECT_APPROVAL", None,
                               ["scope_or_budget_mismatch"], masked, simulate)
    if request.get("evidence_required") and not request.get("evidence_satisfied"):
        return await _decision(conn, tenant_id, decision_id, "PROJECT_APPROVAL", str(grant["id"]),
                               ["evidence_required"], masked, simulate)
    # The locked grant serializes each reservation; accumulated reservations
    # are checked before consumption so all bounded budgets remain atomic.
    consumed = await conn.fetchrow(
        """SELECT COALESCE(sum((budget_delta->>'files')::bigint),0) AS files,
                  COALESCE(sum((budget_delta->>'rows')::bigint),0) AS rows,
                  COALESCE(sum((budget_delta->>'cost_usd')::numeric),0) AS cost_usd,
                  COALESCE(sum((budget_delta->>'duration_seconds')::bigint),0) AS duration_seconds
             FROM goal_auto_approval_uses
            WHERE tenant_id=$1::uuid AND grant_id=$2::uuid
              AND status IN ('reserved','executing','completed','failed','manual_reconciliation')""",
        tenant_id, grant["id"],
    )
    budget = {"files": int(request.get("estimated_files", 0)), "rows": int(request.get("estimated_rows", 0)),
              "cost_usd": float(request.get("estimated_cost_usd", 0)),
              "duration_seconds": int(request.get("estimated_duration_seconds", 0))}
    exceeded = [
        name for name, limit, used, delta in (
            ("max_files_exhausted", int(grant["max_files"]), int(consumed["files"]), budget["files"]),
            ("max_rows_exhausted", int(grant["max_rows"]), int(consumed["rows"]), budget["rows"]),
            ("max_cost_usd_exhausted", float(grant["max_cost_usd"]), float(consumed["cost_usd"]), budget["cost_usd"]),
            ("max_duration_seconds_exhausted", int(grant["max_duration_seconds"]),
             int(consumed["duration_seconds"]), budget["duration_seconds"]),
        ) if used + delta > limit
    ]
    if exceeded:
        return await _decision(conn, tenant_id, decision_id, "PROJECT_APPROVAL", str(grant["id"]),
                               exceeded, masked, simulate)
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
    await conn.execute(
        """INSERT INTO goal_auto_approval_uses
           (tenant_id,decision_id,execution_key,grant_id,grant_version,input_hash,target_type,target_id,
            target_version,patch_hash,action,budget_delta,status,correlation_id)
           VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5,$6,$7,$8::uuid,$9,$10,$11,$12::jsonb,'reserved',$13::uuid)""",
        tenant_id, decision_id, request["execution_key"], grant["id"], grant["grant_version"],
        request_hash, request["target_type"], request["target_id"], request["target_version"],
        request.get("patch_hash"), request["action"], json.dumps(budget), request.get("correlation_id") or str(uuid4()),
    )
    await conn.execute(
        """INSERT INTO goal_auto_approval_usage_events
             (tenant_id,use_id,execution_key,grant_id,grant_version,event_type,sequence_no,estimated_budget)
           SELECT tenant_id,id,execution_key,grant_id,grant_version,'reserved',1,budget_delta
             FROM goal_auto_approval_uses WHERE tenant_id=$1::uuid AND execution_key=$2""",
        tenant_id, request["execution_key"],
    )
    return await _decision(conn, tenant_id, decision_id, "AUTO", str(grant["id"]),
                           ["grant_reserved"], masked, False, remaining_uses=int(grant["max_executions"])-int(updated))


async def reconcile_grant_use(
    conn: Any, *, tenant_id: str, execution_key: str, actual_budget: Mapping[str, Any],
    outcome: str, actor: ActorScope,
) -> dict[str, Any]:
    """Append the measured outcome; unknown effects are never refunded or replayed."""
    use = await conn.fetchrow(
        """SELECT u.*,g.max_files,g.max_rows,g.max_cost_usd,g.max_duration_seconds
             FROM goal_auto_approval_uses u JOIN goal_auto_approval_grants g
               ON g.id=u.grant_id AND g.tenant_id=u.tenant_id
            WHERE u.tenant_id=$1::uuid AND u.execution_key=$2 FOR UPDATE OF u,g""",
        tenant_id, execution_key,
    )
    if not use:
        raise _error(404, "grant_use_not_found")
    grant_project = await conn.fetchval(
        "SELECT project FROM goal_auto_approval_grants WHERE tenant_id=$1::uuid AND id=$2::uuid",
        tenant_id, use["grant_id"],
    )
    if not grant_project or not actor.may_access(str(grant_project)):
        raise _error(403, "project_scope_denied")
    if use["status"] in {"completed", "manual_reconciliation"}:
        return _row_dict(use)
    actual = {"files": int(actual_budget.get("files", 0)), "rows": int(actual_budget.get("rows", 0)),
              "cost_usd": float(actual_budget.get("cost_usd", 0)),
              "duration_seconds": int(actual_budget.get("duration_seconds", 0))}
    estimated = use["budget_delta"]
    if isinstance(estimated, str):
        estimated = json.loads(estimated)
    overrun = any(actual[name] > float(use[limit]) or actual[name] > float(estimated.get(name, 0))
                  for name, limit in (
        ("files", "max_files"), ("rows", "max_rows"), ("cost_usd", "max_cost_usd"),
        ("duration_seconds", "max_duration_seconds")))
    unknown = outcome == "unknown"
    status = "manual_reconciliation" if unknown or overrun else ("completed" if outcome == "completed" else "failed")
    result = {"outcome": outcome, "actual_budget": actual, "budget_overrun": overrun,
              "auto_refund": False, "auto_replay": False}
    row = await conn.fetchrow(
        """UPDATE goal_auto_approval_uses SET status=$3,result=$4::jsonb,completed_at=clock_timestamp()
             WHERE tenant_id=$1::uuid AND execution_key=$2 RETURNING *""",
        tenant_id, execution_key, status, json.dumps(result),
    )
    if overrun:
        await conn.execute(
            """UPDATE goal_auto_approval_grants SET status='stale',revoked_at=clock_timestamp(),
                      revoke_reason='budget_overrun' WHERE tenant_id=$1::uuid AND id=$2::uuid AND status='active'""",
            tenant_id, use["grant_id"],
        )
    await conn.execute(
        """INSERT INTO goal_auto_approval_usage_events
             (tenant_id,use_id,execution_key,grant_id,grant_version,event_type,sequence_no,actual_budget,diagnostics)
           SELECT u.tenant_id,u.id,u.execution_key,u.grant_id,u.grant_version,
                  $3,(SELECT COALESCE(max(sequence_no),0)+1 FROM goal_auto_approval_usage_events e
                       WHERE e.tenant_id=u.tenant_id AND e.execution_key=u.execution_key),$4::jsonb,$5::jsonb
             FROM goal_auto_approval_uses u
            WHERE u.tenant_id=$1::uuid AND u.execution_key=$2""",
        tenant_id, execution_key, "reconciliation_required" if status == "manual_reconciliation" else
        ("budget_overrun" if overrun else status), json.dumps(actual), json.dumps(result),
    )
    return _row_dict(row)


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
           revoked_at=clock_timestamp(),revoke_reason=$4
           WHERE id=$1::uuid AND tenant_id=$2::uuid
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
        """WITH RECURSIVE descendants AS (
             SELECT id FROM goal_auto_approval_grants WHERE parent_grant_id=$1::uuid AND tenant_id=$3::uuid
             UNION ALL SELECT g.id FROM goal_auto_approval_grants g
               JOIN descendants d ON g.parent_grant_id=d.id WHERE g.tenant_id=$3::uuid
           ) UPDATE goal_auto_approval_grants SET status='revoked',revoked_by=$2::uuid,
             revoked_at=clock_timestamp(),revoke_reason='parent_grant_revoked'
             WHERE id IN (SELECT id FROM descendants) AND status='active'""",
        grant_id, actor.session_id, tenant_id,
    )
    return _row_dict(row)


async def submit_review(conn: Any, *, tenant_id: str, item_id: str, actor: ActorScope) -> dict[str, Any]:
    item = await _target(conn, tenant_id, item_id)
    if not actor.may_access(str(item["project"])):
        raise _error(403, "project_scope_denied")
    if item["status"] not in {"ready", "in_progress", "changes_requested"}:
        raise _error(409, "invalid_review_transition")
    missing = await conn.fetchval(
        "SELECT NOT EXISTS(SELECT 1 FROM work_item_evidence WHERE work_item_id=$1::uuid AND tenant_id=$2::uuid)",
        item_id, tenant_id,
    )
    children_open = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM work_items
             WHERE parent_id=$1::uuid AND tenant_id=$2::uuid AND status<>'completed')""", item_id, tenant_id,
    )
    if missing or children_open:
        raise _error(422, "evidence_required")
    row = await conn.fetchrow(
        """UPDATE work_items SET status='in_review',version=version+1,updated_at=clock_timestamp()
             WHERE id=$1::uuid AND tenant_id=$2::uuid AND status IN ('ready','in_progress','changes_requested')
             RETURNING *""", item_id, tenant_id,
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
