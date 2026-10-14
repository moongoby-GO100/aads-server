"""Server-verified role assignment for the canonical goal work hierarchy.

One goal-level change set binds (a) the CEO approval evidence, (b) the exact
work-item ID set with each item's recommended role and (c) the role holders.
Execution re-derives all three from the database; nothing the caller sends is
trusted beyond the references that point at server records.

The path reuses the M14 change-set, route, decision and event tables.  It never
touches ``work_items.created_by``/``status`` and never creates grants.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.services.goal_policy_foundation import patch_hash as canonical_patch_hash
from app.services.goal_policy_foundation import validate_patch
from app.services.goal_work_hierarchy import ActorScope, _row_dict
from app.services.goal_workflow_approval import (
    _change_set_body_hash,
    _require_enabled,
    append_event,
    canonical_hash,
    decide_change_set,
    route_change_set,
)

ROLES = ("PM", "Developer", "QA")
_ROLE_RE = re.compile(r"권장 역할:\s*(PM|Developer|QA)\b")
_ASSIGN_ACTION = "assign"
_CEO_WORKSPACE = "[CEO] 통합지시"
_LIVE_STATES = ("pending", "approved", "executing", "executed")


def _error(status: int, code: str, message: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message or code})


def recommended_role(description: str | None) -> str | None:
    """Return the single ``권장 역할`` in a description, or None if absent/ambiguous."""
    found = _ROLE_RE.findall(description or "")
    return found[0] if len(found) == 1 and (description or "").count("권장 역할") == 1 else None


def ids_fingerprint(ids: Sequence[str]) -> str:
    """md5 of the comma-joined sorted IDs (same fingerprint as the registration result)."""
    return hashlib.md5(",".join(sorted(str(i) for i in ids)).encode(), usedforsecurity=False).hexdigest()


def goal_role_key(role: str, goal_id: str) -> str:
    return f"{role}@{str(goal_id)[:8]}"


def _uuid(value: Any, code: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise _error(400, code) from None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha256(value: str | None) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()


async def _lock(conn: Any, key: str) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", key)


async def _session_scope(conn: Any, tenant_id: str, session_id: str) -> Any:
    return await conn.fetchrow(
        """SELECT s.id::text AS session_id, COALESCE(s.role_key,'') AS role_key,
                  s.user_id::text AS user_id, COALESCE(w.project_key,'') AS project,
                  COALESCE(w.name,'') AS workspace_name,
                  COALESCE(w.display_name,'') AS display_name
             FROM chat_sessions s
             JOIN chat_workspaces w ON w.id=s.workspace_id AND w.tenant_id=s.tenant_id
            WHERE s.id=$1::uuid AND s.tenant_id=$2::uuid""",
        session_id, tenant_id,
    )


def _is_ceo_integrated(row: Any) -> bool:
    return _text(row["project"]).upper() == "CEO" and _CEO_WORKSPACE in {
        _text(row["workspace_name"]), _text(row["display_name"]),
    }


async def verify_approval_evidence(
    conn: Any, *, tenant_id: str, goal_id: str, decision_ref: str, approval_message_id: str,
) -> dict[str, Any]:
    """Resolve CEO approval evidence from server records or fail closed.

    A blank reference, a decision of another tenant/project/goal, a withdrawn
    or superseded decision, a deleted/hidden/foreign message, or a message
    whose session is not owned by a tenant owner all raise.  The returned
    dict is the exact value bound into the change-set patch.
    """
    ref = _text(decision_ref)
    if not ref or not _text(approval_message_id):
        raise _error(400, "approval_ref_required")
    message_id = _uuid(approval_message_id, "approval_ref_invalid")
    goal = await conn.fetchrow(
        "SELECT id::text AS id, project FROM goals WHERE id=$1::uuid AND tenant_id=$2::uuid",
        goal_id, tenant_id,
    )
    if not goal:
        raise _error(404, "goal_not_found")
    decision = await conn.fetchrow(
        """SELECT id::text AS id, entry_type, status, revision, body, metadata,
                  source_session_id::text AS source_session_id, created_at
             FROM project_handover_entries
            WHERE tenant_id=$1::uuid AND project_key=$2 AND entry_key=$3""",
        tenant_id, goal["project"], ref,
    )
    if not decision or decision["entry_type"] != "decision":
        raise _error(403, "approval_decision_not_found")
    if decision["status"] != "active":
        raise _error(409, "approval_withdrawn")
    meta = decision["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    meta = dict(meta or {})
    if _text(meta.get("goal_id")) != str(goal_id) or _text(meta.get("approval_message_id")) != message_id:
        raise _error(403, "approval_ref_mismatch")
    if meta.get("deploy_allowed") is not False:
        raise _error(403, "approval_scope_exceeded")
    roles = meta.get("roles")
    if not isinstance(roles, Mapping) or set(roles) != set(ROLES) or any(
        not isinstance(roles[r], int) or isinstance(roles[r], bool) or roles[r] < 0 for r in ROLES
    ):
        raise _error(403, "approval_roles_invalid")
    source = _text(decision["source_session_id"])
    if not source:
        raise _error(403, "approval_source_session_missing")
    session = await _session_scope(conn, tenant_id, source)
    if not session or not _is_ceo_integrated(session) or _text(session["role_key"]).lower() != "ceo":
        raise _error(403, "approval_source_not_ceo")
    message = await conn.fetchrow(
        """SELECT id::text AS id, session_id::text AS session_id, role, content,
                  content_version, created_at
             FROM chat_messages
            WHERE id=$1::uuid AND tenant_id=$2::uuid AND deleted_at IS NULL
              AND COALESCE(is_hidden,false)=false""",
        message_id, tenant_id,
    )
    if not message:
        raise _error(409, "approval_message_withdrawn")
    if message["session_id"] != source or message["role"] != "user" or not _text(message["content"]):
        raise _error(403, "approval_message_mismatch")
    if message["created_at"] > decision["created_at"]:
        raise _error(403, "approval_message_after_decision")
    owner = await conn.fetchval(
        """SELECT role FROM tenant_memberships
            WHERE tenant_id=$1::uuid AND user_id=$2 AND status='active' AND deleted_at IS NULL""",
        tenant_id, session["user_id"],
    )
    if owner != "owner":
        raise _error(403, "approval_actor_not_owner")
    return {
        "decision_ref": ref,
        "decision_id": decision["id"],
        "decision_revision": int(decision["revision"]),
        "decision_body_sha256": _sha256(decision["body"]),
        "approval_message_id": message_id,
        "message_content_version": int(message["content_version"] or 0),
        "message_content_sha256": _sha256(message["content"]),
        "source_session_id": source,
        "approver_user_id": session["user_id"],
        "role_counts": {r: int(roles[r]) for r in ROLES},
    }


def _no_impersonation(actor: ActorScope, evidence: Mapping[str, Any]) -> None:
    """Only the approving owner may drive the approver's session."""
    if actor.session_id == evidence["source_session_id"] and not (
        actor.owner_verified and actor.user_id == evidence["approver_user_id"]
    ):
        raise _error(403, "approver_session_impersonation")


async def load_goal_items(conn: Any, *, tenant_id: str, goal_id: str, project: str, lock: bool) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"""SELECT id::text AS id, type, status, version, assignment_id::text AS assignment_id,
                   created_by::text AS created_by, description
              FROM work_items
             WHERE tenant_id=$1::uuid AND project=$2 AND goal_id=$3::uuid
             ORDER BY id{' FOR UPDATE' if lock else ''}""",
        tenant_id, project, goal_id,
    )
    return [dict(r) for r in rows]


def build_item_plan(items: Sequence[Mapping[str, Any]], role_counts: Mapping[str, int]) -> dict[str, list[str]]:
    """Group item IDs by the role recorded in each description; fail on any drift."""
    by_role: dict[str, list[str]] = {r: [] for r in ROLES}
    for item in items:
        role = recommended_role(item.get("description"))
        if role is None:
            raise _error(409, "target_set_mismatch", f"item {item['id']} has no unique recommended role")
        by_role[role].append(str(item["id"]))
    if {r: len(v) for r, v in by_role.items()} != {r: int(role_counts[r]) for r in ROLES}:
        raise _error(409, "target_set_mismatch", "role counts differ from the approved decision")
    return {r: sorted(v) for r, v in by_role.items()}


async def _resolve_holder(
    conn: Any, *, tenant_id: str, project: str, goal_id: str, role: str, session_id: str,
    forbidden_sessions: Sequence[str],
) -> dict[str, Any]:
    sid = _uuid(session_id, "holder_session_invalid")
    if sid in forbidden_sessions:
        raise _error(403, "holder_session_forbidden")
    row = await _session_scope(conn, tenant_id, sid)
    if not row or _text(row["project"]).upper() != project.upper() or _is_ceo_integrated(row):
        raise _error(403, "project_scope_denied", f"{role} holder session is outside the goal project")
    existing = await conn.fetchrow(
        """SELECT id::text AS id, role_key FROM project_role_assignments
            WHERE tenant_id=$1::uuid AND project=$2 AND session_id=$3::uuid AND active""",
        tenant_id, project, sid,
    )
    scoped = goal_role_key(role, goal_id)
    if existing:
        if _text(existing["role_key"]).lower() not in {role.lower(), scoped.lower()}:
            raise _error(409, "holder_role_conflict", f"session already holds {existing['role_key']}")
        return {"mode": "existing", "assignment_id": existing["id"], "role_key": existing["role_key"],
                "holder_session_id": sid}
    if _text(row["role_key"]).lower() != role.lower():
        raise _error(403, "holder_role_mismatch", f"session role_key is not {role}")
    taken = await conn.fetchval(
        """SELECT session_id::text FROM project_role_assignments
            WHERE tenant_id=$1::uuid AND project=$2 AND role_key=$3 AND active""",
        tenant_id, project, scoped,
    )
    if taken and taken != sid:
        raise _error(409, "role_holder_conflict", f"{scoped} is held by another session")
    return {"mode": "new", "assignment_id": None, "role_key": scoped, "holder_session_id": sid}


def build_patch(
    evidence: Mapping[str, Any], goal_id: str, plan: Mapping[str, Sequence[str]],
    holders: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [{"op": "test", "path": "/approval", "value": dict(evidence)}]
    for role in ROLES:
        ids = list(plan[role])
        ops.append({"op": "add", "path": f"/assignments/{role}", "value": {
            "item_ids": ids, "count": len(ids), **{k: holders[role][k] for k in
                                                   ("mode", "assignment_id", "role_key", "holder_session_id")},
        }})
    every = [i for role in ROLES for i in plan[role]]
    ops.append({"op": "test", "path": "/scope", "value": {
        "goal_id": str(goal_id), "total": len(every), "ids_md5": ids_fingerprint(every),
        "item_version": 1,
    }})
    return ops


def _patch_parts(patch: Sequence[Mapping[str, Any]]) -> tuple[dict, dict[str, dict], dict]:
    try:
        validate_patch(list(patch))
        approval = next(o["value"] for o in patch if o["path"] == "/approval")
        scope = next(o["value"] for o in patch if o["path"] == "/scope")
        assignments = {role: next(o["value"] for o in patch if o["path"] == f"/assignments/{role}")
                       for role in ROLES}
    except (StopIteration, KeyError, TypeError, ValueError):
        raise _error(422, "invalid_patch") from None
    return approval, assignments, scope


async def _goal_and_evidence(
    conn: Any, *, tenant_id: str, actor: ActorScope, goal_id: str, decision_ref: str,
    approval_message_id: str,
) -> tuple[Any, dict[str, Any]]:
    goal = await conn.fetchrow(
        "SELECT id::text AS id, project FROM goals WHERE id=$1::uuid AND tenant_id=$2::uuid",
        goal_id, tenant_id,
    )
    if not goal:
        raise _error(404, "goal_not_found")
    if not actor.may_access(str(goal["project"])):
        raise _error(403, "project_scope_denied")
    evidence = await verify_approval_evidence(
        conn, tenant_id=tenant_id, goal_id=goal_id, decision_ref=decision_ref,
        approval_message_id=approval_message_id,
    )
    _no_impersonation(actor, evidence)
    return goal, evidence


async def _plan_and_holders(
    conn: Any, *, tenant_id: str, goal_id: str, goal: Any, evidence: Mapping[str, Any],
    holders: Mapping[str, str],
) -> dict[str, Any]:
    items = await load_goal_items(conn, tenant_id=tenant_id, goal_id=goal_id, project=goal["project"], lock=False)
    if any(i["status"] != "draft" or i["assignment_id"] for i in items):
        raise _error(409, "target_set_mismatch", "every item must be an unassigned draft")
    plan = build_item_plan(items, evidence["role_counts"])
    if set(holders) != set(ROLES):
        raise _error(400, "holders_incomplete")
    if len({_text(holders[r]) for r in ROLES}) != len(ROLES):
        raise _error(400, "holder_duplicate")
    resolved = {
        role: await _resolve_holder(
            conn, tenant_id=tenant_id, project=goal["project"], goal_id=goal_id, role=role,
            session_id=holders[role], forbidden_sessions=[evidence["source_session_id"]],
        )
        for role in ROLES
    }
    return {"goal_id": goal_id, "project": goal["project"], "evidence": dict(evidence), "plan": plan,
            "holders": resolved, "patch": build_patch(evidence, goal_id, plan, resolved)}


async def preview_assignment(
    conn: Any, *, tenant_id: str, actor: ActorScope, goal_id: str, decision_ref: str,
    approval_message_id: str, holders: Mapping[str, str],
) -> dict[str, Any]:
    """Read-only: verify evidence, derive the ID set and resolve holders."""
    goal, evidence = await _goal_and_evidence(
        conn, tenant_id=tenant_id, actor=actor, goal_id=goal_id, decision_ref=decision_ref,
        approval_message_id=approval_message_id,
    )
    return await _plan_and_holders(
        conn, tenant_id=tenant_id, goal_id=goal_id, goal=goal, evidence=evidence, holders=holders,
    )


async def propose_assignment(
    conn: Any, *, tenant_id: str, actor: ActorScope, goal_id: str, decision_ref: str,
    approval_message_id: str, holders: Mapping[str, str],
) -> dict[str, Any]:
    _require_enabled()
    await _lock(conn, f"canonical-assign:{tenant_id}:{goal_id}")
    goal, evidence = await _goal_and_evidence(
        conn, tenant_id=tenant_id, actor=actor, goal_id=goal_id, decision_ref=decision_ref,
        approval_message_id=approval_message_id,
    )
    project = str(goal["project"])
    key = f"canonical-assign:{goal_id}:{evidence['decision_id']}:{evidence['decision_revision']}"
    existing = await conn.fetchrow(
        "SELECT * FROM work_item_change_sets WHERE tenant_id=$1::uuid AND idempotency_key=$2",
        tenant_id, key,
    )
    if existing:
        stored_approval, stored_assignments, _ = _patch_parts(_patch_of(existing))
        same_holders = {r: stored_assignments[r]["holder_session_id"] for r in ROLES} == {
            r: _text(holders.get(r)) for r in ROLES
        }
        if stored_approval != evidence or not same_holders:
            raise _error(409, "idempotency_conflict")
        return {"change_set": _row_dict(existing), "replayed": True, "evidence": evidence}
    reused = await conn.fetchval(
        """SELECT id::text FROM work_item_change_sets
            WHERE tenant_id=$1::uuid AND action='assign' AND target_type='goal'
              AND state = ANY($2::text[])
              AND patch @> $3::jsonb AND idempotency_key <> $4 LIMIT 1""",
        tenant_id, list(_LIVE_STATES),
        json.dumps([{"op": "test", "path": "/approval", "value": {"decision_id": evidence["decision_id"]}}]),
        key,
    )
    if reused:
        raise _error(409, "approval_ref_already_used")
    preview = await _plan_and_holders(
        conn, tenant_id=tenant_id, goal_id=goal_id, goal=goal, evidence=evidence, holders=holders,
    )
    patch = preview["patch"]
    total = sum(len(v) for v in preview["plan"].values())
    payload = {
        "action": _ASSIGN_ACTION, "base_version": 1, "patch": patch, "environment": "dev",
        "rationale": f"CEO 승인 {evidence['decision_ref']} 의 권장 역할 배정",
        "expected_effect": f"{total}건 assignment_id 연결(상태·created_by 불변)",
        "rollback_plan": "롤백 change-set 으로 assignment_id 를 비우고 신규 assignment 를 종료",
    }
    body_hash = _change_set_body_hash(goal_id, payload)
    patch_hash = canonical_patch_hash(patch)
    row = await conn.fetchrow(
        """INSERT INTO work_item_change_sets
           (tenant_id,project,target_type,target_id,action,base_version,target_version,patch,
            patch_hash,body_hash,risk_factors,rationale,expected_effect,rollback_plan,risk_tier,
            state,idempotency_key,requested_by,environment)
           VALUES($1::uuid,$2,'goal',$3::uuid,'assign',1,2,$4::jsonb,$5,$6,'{}'::text[],$7,$8,$9,
                  'A3','pending',$10,$11::uuid,'dev')
           RETURNING *""",
        tenant_id, project, goal_id, json.dumps(patch), patch_hash, body_hash,
        payload["rationale"], payload["expected_effect"], payload["rollback_plan"], key, actor.session_id,
    )
    cs_id = str(row["id"])
    await append_event(
        conn, tenant_id=tenant_id, project=project, aggregate_type="change_set", aggregate_id=cs_id,
        event_type="canonical_assignment_proposed", actor=actor,
        payload={"patch_hash": patch_hash, "decision_id": evidence["decision_id"],
                 "approval_message_id": evidence["approval_message_id"],
                 "ids_md5": patch[-1]["value"]["ids_md5"],
                 "holder_modes": {r: preview["holders"][r]["mode"] for r in ROLES}},
        correlation_id=str(uuid4()),
    )
    # The CEO decision already authorises this scope and no reviewer role
    # exists for the goal, so the only route is the owner-verified CEO.
    routing = await route_change_set(
        conn, tenant_id=tenant_id, change_set_id=cs_id, actor=actor, routes=["ceo"],
    )
    return {"change_set": _row_dict(row), "replayed": False, "evidence": evidence, **routing}


async def decide_assignment(
    conn: Any, *, tenant_id: str, actor: ActorScope, change_set_id: str, approve: bool, reason: str,
) -> dict[str, Any]:
    """CEO decision on the assignment change set; approval re-checks live evidence."""
    row = await _load_change_set(conn, tenant_id, change_set_id)
    approval, _, _ = _patch_parts(_patch_of(row))
    if not actor.may_access(str(row["project"])):
        raise _error(403, "project_scope_denied")
    if approve:
        live = await verify_approval_evidence(
            conn, tenant_id=tenant_id, goal_id=str(row["target_id"]),
            decision_ref=approval["decision_ref"], approval_message_id=approval["approval_message_id"],
        )
        if live != approval:
            raise _error(409, "approval_evidence_changed")
        if actor.session_id != live["source_session_id"]:
            raise _error(403, "ceo_approval_required")
    return await decide_change_set(
        conn, tenant_id=tenant_id, change_set_id=change_set_id, actor=actor, approve=approve, reason=reason,
    )


async def _approved_by_owner(conn: Any, tenant_id: str, change_set_id: str, evidence: Mapping[str, Any]) -> None:
    decision = await conn.fetchrow(
        """SELECT d.actor_session_id::text AS actor_session_id, d.decision
             FROM work_item_change_set_approval_decisions d
             JOIN work_item_change_set_approval_routes r ON r.id=d.route_id
            WHERE d.tenant_id=$1::uuid AND d.change_set_id=$2::uuid AND r.route_key='ceo'""",
        tenant_id, change_set_id,
    )
    if not decision or decision["decision"] != "approved" or decision["actor_session_id"] != evidence["source_session_id"]:
        raise _error(409, "approval_required", "no owner-session CEO decision recorded")
    event = await conn.fetchrow(
        """SELECT payload FROM work_item_events
            WHERE tenant_id=$1::uuid AND aggregate_id=$2::uuid
              AND event_type='change_set_route_approved' AND actor_session_id=$3::uuid
            ORDER BY created_at DESC LIMIT 1""",
        tenant_id, change_set_id, evidence["source_session_id"],
    )
    payload = event["payload"] if event else None
    if isinstance(payload, str):
        payload = json.loads(payload)
    principal = (payload or {}).get("principal") or {}
    if principal.get("owner_verified") is not True or principal.get("user_id") != evidence["approver_user_id"]:
        raise _error(403, "ceo_owner_verification_required")


async def _load_change_set(conn: Any, tenant_id: str, change_set_id: str) -> Any:
    row = await conn.fetchrow(
        "SELECT * FROM work_item_change_sets WHERE id=$1::uuid AND tenant_id=$2::uuid FOR UPDATE",
        change_set_id, tenant_id,
    )
    if not row or row["target_type"] != "goal" or row["action"] != _ASSIGN_ACTION:
        raise _error(404, "change_set_not_found")
    return row


def _patch_of(row: Any) -> list[dict[str, Any]]:
    patch = row["patch"]
    return json.loads(patch) if isinstance(patch, str) else list(patch)


async def execute_assignment(
    conn: Any, *, tenant_id: str, actor: ActorScope, change_set_id: str,
    decision_ref: str, approval_message_id: str, execution_key: str,
) -> dict[str, Any]:
    """Apply the approved assignment exactly once; replays return the first result."""
    _require_enabled()
    if not _text(execution_key):
        raise _error(400, "execution_key_required")
    row = await _load_change_set(conn, tenant_id, change_set_id)
    goal_id, project = str(row["target_id"]), str(row["project"])
    if not actor.may_access(project):
        raise _error(403, "project_scope_denied")
    await _lock(conn, f"canonical-assign:{tenant_id}:{goal_id}")
    patch = _patch_of(row)
    approval, assignments, scope = _patch_parts(patch)
    _no_impersonation(actor, approval)
    if row["execution_key"]:
        if row["execution_key"] != execution_key:
            raise _error(409, "execution_key_conflict")
        return {"change_set_id": change_set_id, "state": row["state"], "execution_key": execution_key,
                "replayed": True, "total": scope["total"]}
    if row["state"] in {"revoked", "expired", "superseded", "rejected"}:
        raise _error(409, "approval_revoked")
    if row["state"] != "approved":
        raise _error(409, "approval_required")
    if canonical_patch_hash(patch) != row["patch_hash"]:
        await append_event(
            conn, tenant_id=tenant_id, project=project, aggregate_type="change_set",
            aggregate_id=change_set_id, event_type="patch_tamper_blocked", actor=actor,
            payload={"stored_patch_hash": row["patch_hash"]}, correlation_id=str(uuid4()),
        )
        raise _error(409, "patch_hash_mismatch")
    if _text(decision_ref) != approval["decision_ref"] or _text(approval_message_id) != approval["approval_message_id"]:
        raise _error(403, "approval_ref_mismatch")
    live = await verify_approval_evidence(
        conn, tenant_id=tenant_id, goal_id=goal_id, decision_ref=decision_ref,
        approval_message_id=approval_message_id,
    )
    if live != approval:
        raise _error(409, "approval_evidence_changed")
    await _approved_by_owner(conn, tenant_id, change_set_id, live)

    items = await load_goal_items(conn, tenant_id=tenant_id, goal_id=goal_id, project=project, lock=True)
    plan = build_item_plan(items, live["role_counts"])
    every = [i for role in ROLES for i in plan[role]]
    if every != [i for role in ROLES for i in assignments[role]["item_ids"]] or ids_fingerprint(every) != scope["ids_md5"]:
        raise _error(409, "target_set_mismatch", "goal items differ from the approved ID set")
    if any(i["status"] != "draft" or i["assignment_id"] or int(i["version"]) != int(scope["item_version"]) for i in items):
        raise _error(409, "target_set_mismatch", "items are no longer unassigned drafts at the approved version")

    created: list[str] = []
    resolved: dict[str, str] = {}
    for role in ROLES:
        want = assignments[role]
        holder = await _resolve_holder(
            conn, tenant_id=tenant_id, project=project, goal_id=goal_id, role=role,
            session_id=want["holder_session_id"], forbidden_sessions=[live["source_session_id"]],
        )
        if holder["mode"] != want["mode"] or holder["role_key"] != want["role_key"] or (
            holder["assignment_id"] != want["assignment_id"]
        ):
            raise _error(409, "holder_changed", f"{role} holder differs from the approved proposal")
        if holder["mode"] == "existing":
            resolved[role] = holder["assignment_id"]
            continue
        made = await conn.fetchrow(
            """INSERT INTO project_role_assignments(tenant_id,project,role_key,session_id,assigned_by)
               VALUES($1::uuid,$2,$3,$4::uuid,$5::uuid) RETURNING id::text AS id""",
            tenant_id, project, holder["role_key"], holder["holder_session_id"], actor.session_id,
        )
        resolved[role] = made["id"]
        created.append(made["id"])
    applied = 0
    for role in ROLES:
        updated = await conn.fetch(
            """UPDATE work_items SET assignment_id=$1::uuid, version=version+1, updated_at=clock_timestamp()
                WHERE tenant_id=$2::uuid AND project=$3 AND goal_id=$4::uuid
                  AND id = ANY($5::uuid[]) AND version=$6 AND assignment_id IS NULL
                  AND status='draft' RETURNING id""",
            resolved[role], tenant_id, project, goal_id, plan[role], int(scope["item_version"]),
        )
        if len(updated) != len(plan[role]):
            raise _error(409, "target_version_changed")
        applied += len(updated)
    await conn.execute(
        """UPDATE work_item_change_sets SET state='executed', execution_key=$2,
               executed_at=clock_timestamp(), updated_at=clock_timestamp() WHERE id=$1::uuid""",
        change_set_id, execution_key,
    )
    await append_event(
        conn, tenant_id=tenant_id, project=project, aggregate_type="change_set",
        aggregate_id=change_set_id, event_type="canonical_assignment_executed", actor=actor,
        payload={
            "execution_key": execution_key, "applied": applied, "ids_md5": scope["ids_md5"],
            "approver_session_id": live["source_session_id"], "approver_user_id": live["approver_user_id"],
            "decision_id": live["decision_id"], "approval_message_id": live["approval_message_id"],
            "assignments": resolved, "created_assignment_ids": created,
            "role_counts": {r: len(plan[r]) for r in ROLES},
        },
        correlation_id=str(uuid4()),
    )
    return {"change_set_id": change_set_id, "state": "executed", "execution_key": execution_key,
            "replayed": False, "applied": applied, "assignments": resolved,
            "created_assignment_ids": created, "total": applied}


async def rollback_assignment(
    conn: Any, *, tenant_id: str, actor: ActorScope, change_set_id: str, rollback_key: str, reason: str,
) -> dict[str, Any]:
    """Undo an executed assignment through an official rollback change-set record."""
    _require_enabled()
    if not _text(rollback_key) or not _text(reason):
        raise _error(400, "rollback_key_and_reason_required")
    row = await _load_change_set(conn, tenant_id, change_set_id)
    goal_id, project = str(row["target_id"]), str(row["project"])
    if not actor.may_access(project):
        raise _error(403, "project_scope_denied")
    await _lock(conn, f"canonical-assign:{tenant_id}:{goal_id}")
    approval, assignments, scope = _patch_parts(_patch_of(row))
    if actor.session_id != approval["source_session_id"] or not (
        actor.owner_verified and actor.user_id == approval["approver_user_id"]
    ):
        raise _error(403, "rollback_owner_required")
    rb_key = f"canonical-assign-rollback:{change_set_id}"
    prior = await conn.fetchrow(
        "SELECT * FROM work_item_change_sets WHERE tenant_id=$1::uuid AND idempotency_key=$2",
        tenant_id, rb_key,
    )
    if prior:
        if prior["execution_key"] != rollback_key:
            raise _error(409, "execution_key_conflict")
        return {"change_set_id": change_set_id, "rollback_change_set_id": str(prior["id"]),
                "state": row["state"], "replayed": True}
    if row["state"] != "executed":
        raise _error(409, "rollback_not_applicable")
    event = await conn.fetchrow(
        """SELECT payload FROM work_item_events
            WHERE tenant_id=$1::uuid AND aggregate_id=$2::uuid
              AND event_type='canonical_assignment_executed' ORDER BY created_at DESC LIMIT 1""",
        tenant_id, change_set_id,
    )
    payload = event["payload"] if event else None
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not payload:
        raise _error(409, "rollback_record_missing")
    resolved, created = dict(payload["assignments"]), list(payload["created_assignment_ids"])
    items = await load_goal_items(conn, tenant_id=tenant_id, goal_id=goal_id, project=project, lock=True)
    ours = {i for role in ROLES for i in assignments[role]["item_ids"]}
    if {i["id"] for i in items} != ours:
        raise _error(409, "target_set_mismatch")
    for item in items:
        role = next(r for r in ROLES if item["id"] in set(assignments[r]["item_ids"]))
        if item["status"] != "draft":
            raise _error(409, "rollback_blocked_started", "item left draft; refusing to rewind it")
        if item["assignment_id"] not in {resolved[role], None}:
            raise _error(409, "rollback_target_changed")
    restored = 0
    for role in ROLES:
        rows = await conn.fetch(
            """UPDATE work_items SET assignment_id=NULL, version=version+1, updated_at=clock_timestamp()
                WHERE tenant_id=$1::uuid AND project=$2 AND goal_id=$3::uuid
                  AND id = ANY($4::uuid[]) AND assignment_id=$5::uuid AND status='draft' RETURNING id""",
            tenant_id, project, goal_id, assignments[role]["item_ids"], resolved[role],
        )
        restored += len(rows)
    ended: list[str] = []
    for assignment_id in created:
        still_used = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM work_items WHERE tenant_id=$1::uuid AND assignment_id=$2::uuid)",
            tenant_id, assignment_id,
        )
        if still_used:
            continue
        done = await conn.fetchrow(
            """UPDATE project_role_assignments SET active=false, ended_at=clock_timestamp(), end_reason=$3
                WHERE id=$1::uuid AND tenant_id=$2::uuid AND active RETURNING id::text AS id""",
            assignment_id, tenant_id, f"canonical_assignment_rollback:{change_set_id}",
        )
        if done:
            ended.append(done["id"])
    rb_patch = [{"op": "test", "path": "/rollback_of", "value": {
        "change_set_id": change_set_id, "patch_hash": row["patch_hash"], "ids_md5": scope["ids_md5"],
    }}]
    rb_hash = canonical_patch_hash(rb_patch)
    rollback_row = await conn.fetchrow(
        """INSERT INTO work_item_change_sets
           (tenant_id,project,target_type,target_id,action,base_version,target_version,patch,
            patch_hash,body_hash,risk_factors,rationale,expected_effect,rollback_plan,risk_tier,
            state,idempotency_key,requested_by,decided_by,decided_at,execution_key,executed_at,environment)
           VALUES($1::uuid,$2,'goal',$3::uuid,'assign',2,3,$4::jsonb,$5,$6,'{}'::text[],$7,$8,$9,
                  'A3','executed',$10,$11::uuid,$11::uuid,clock_timestamp(),$12,clock_timestamp(),'dev')
           RETURNING id::text AS id""",
        tenant_id, project, goal_id, json.dumps(rb_patch), rb_hash,
        canonical_hash({"rollback_of": change_set_id, "reason": reason}),
        f"롤백: {reason}", "assignment_id 해제·신규 assignment 종료", "재적용은 새 CEO 승인 결정이 필요",
        rb_key, actor.session_id, rollback_key,
    )
    await conn.execute(
        "UPDATE work_item_change_sets SET state='revoked', updated_at=clock_timestamp() WHERE id=$1::uuid",
        change_set_id,
    )
    await append_event(
        conn, tenant_id=tenant_id, project=project, aggregate_type="change_set",
        aggregate_id=change_set_id, event_type="canonical_assignment_rolled_back", actor=actor,
        payload={"rollback_change_set_id": rollback_row["id"], "restored": restored,
                 "ended_assignment_ids": ended, "reason": reason, "ids_md5": scope["ids_md5"]},
        correlation_id=str(uuid4()),
    )
    return {"change_set_id": change_set_id, "rollback_change_set_id": rollback_row["id"],
            "state": "revoked", "restored": restored, "ended_assignment_ids": ended, "replayed": False}
