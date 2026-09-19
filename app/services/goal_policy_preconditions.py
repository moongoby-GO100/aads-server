"""W-13 server-owned policy inputs and versioned precondition snapshots."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from app.services.goal_work_hierarchy import ActorScope, _error


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _item_or_hidden_audit(
    conn: Any, *, tenant_id: str, item_id: str, actor: ActorScope, action: str
) -> Any:
    item = await conn.fetchrow(
        """SELECT id::text,tenant_id::text,project,goal_id::text,milestone_id::text,
                  parent_id::text,type,status,version,acceptance_criteria,assignment_id::text
             FROM work_items WHERE id=$1::uuid AND tenant_id=$2::uuid""",
        item_id, tenant_id,
    )
    if item:
        return item
    # The second lookup is deliberately used only to produce a tenant-local audit.
    # Its result never changes the external 404 contract.
    exists_elsewhere = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM work_items WHERE id=$1::uuid)", item_id,
    )
    if exists_elsewhere:
        await conn.execute(
            """INSERT INTO work_item_events
                   (tenant_id,project,aggregate_type,aggregate_id,event_type,
                    actor_session_id,actor_role_key,payload,correlation_id)
                 VALUES($1::uuid,$2,'work_item',$3::uuid,'tenant_scope_denied',
                        $4::uuid,$5,$6::jsonb,gen_random_uuid())""",
            tenant_id, actor.project or "unknown", item_id, actor.session_id,
            actor.role_key, json.dumps({"action": action}),
        )
    raise _error(404, "resource_not_found")


async def _local_assignment(
    conn: Any, *, tenant_id: str, project: str, actor: ActorScope,
    assignment_id: str | None,
) -> Any:
    if actor.workspace_kind == "ceo_integrated":
        if not assignment_id:
            raise _error(403, "local_assignment_required")
        # The CEO workspace coordinates across projects, but the execution
        # principal is always the selected project's active local assignment.
        row = await conn.fetchrow(
            """SELECT id::text,session_id::text,role_key
                 FROM project_role_assignments
                WHERE tenant_id=$1::uuid AND project=$2 AND active
                  AND id=$3::uuid""",
            tenant_id, project, assignment_id,
        )
    else:
        # Project actors cannot name another session's assignment.  An optional
        # assignment id only narrows the actor-bound lookup.
        row = await conn.fetchrow(
            """SELECT id::text,session_id::text,role_key
                 FROM project_role_assignments
                WHERE tenant_id=$1::uuid AND project=$2 AND active
                  AND session_id=$3::uuid
                  AND ($4::uuid IS NULL OR id=$4::uuid)""",
            tenant_id, project, actor.session_id, assignment_id,
        )
    if not row:
        code = (
            "local_assignment_required"
            if actor.workspace_kind == "ceo_integrated"
            else "project_scope_denied"
        )
        raise _error(403, code)
    return row


async def compute_preconditions(
    conn: Any, *, tenant_id: str, item_id: str, actor: ActorScope,
    assignment_id: str | None = None, expected_parent_version: int | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Compute exclusively from primary rows; no client precondition fields enter."""
    item = await _item_or_hidden_audit(
        conn, tenant_id=tenant_id, item_id=item_id, actor=actor, action="precondition_preview",
    )
    project = str(item["project"])
    if not actor.may_access(project):
        raise _error(403, "project_scope_denied")
    assignment = await _local_assignment(
        conn, tenant_id=tenant_id, project=project, actor=actor, assignment_id=assignment_id,
    )
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 13))",
        f"{tenant_id}:{item_id}:preconditions",
    )
    parent = None
    if item["parent_id"]:
        parent = await conn.fetchrow(
            """SELECT status,version FROM work_items
                WHERE id=$1::uuid AND tenant_id=$2::uuid AND project=$3""",
            item["parent_id"], tenant_id, project,
        )
        if not parent:
            raise _error(422, "invalid_parent")
    else:
        parent = await conn.fetchrow(
            """SELECT status,version FROM milestones
                WHERE id=$1::uuid AND goal_id=$2::uuid
                  AND tenant_id=$3::uuid AND project=$4""",
            item["milestone_id"], item["goal_id"], tenant_id, project,
        )
        if not parent:
            raise _error(404, "resource_not_found")
    parent_version = int(parent["version"])
    if expected_parent_version is not None and expected_parent_version != parent_version:
        raise _error(409, "version_conflict")

    evidence = await conn.fetch(
        """SELECT id::text,criterion_key,COALESCE(artifact_hash,content_hash) AS artifact_hash,
                  COALESCE(verification_state,CASE WHEN verified THEN 'verified' ELSE 'pending' END) AS state
             FROM work_item_evidence
            WHERE tenant_id=$1::uuid AND project=$2 AND work_item_id=$3::uuid
              AND work_item_version=$4
            ORDER BY criterion_key NULLS LAST,COALESCE(artifact_hash,content_hash),id""",
        tenant_id, project, item_id, item["version"],
    )
    evidence_records = [dict(row) for row in evidence]
    evidence_hash = _canonical_hash({"version": int(item["version"]), "evidence": evidence_records})
    criteria = item["acceptance_criteria"] or []
    if isinstance(criteria, str):
        criteria = json.loads(criteria)
    criterion_keys = {
        str(value.get("key") or value.get("id") or index)
        if isinstance(value, dict)
        else str(index)
        for index, value in enumerate(criteria)
    }
    verified_keys = {
        str(row["criterion_key"])
        for row in evidence_records
        if row["state"] == "verified"
    }
    evidence_complete = not criteria or (
        criterion_keys <= verified_keys
        and all(row["state"] == "verified" for row in evidence_records)
    )

    review = await conn.fetchrow(
        """SELECT verdict FROM work_item_review_decisions
            WHERE tenant_id=$1::uuid AND project=$2 AND work_item_id=$3::uuid
              AND work_item_version=$4 ORDER BY decided_at DESC,id DESC LIMIT 1""",
        tenant_id, project, item_id, item["version"],
    )
    pending_review = await conn.fetchval(
        """SELECT EXISTS(
             SELECT 1 FROM work_item_review_requirements
            WHERE tenant_id=$1::uuid AND project=$2 AND work_item_id=$3::uuid
              AND work_item_version=$4
              AND state IN ('pending_assignment','assigned','in_review'))""",
        tenant_id, project, item_id, item["version"],
    )
    verdict = "none"
    if review:
        verdict = "accepted" if review["verdict"] in {"accepted", "override"} else "rejected"
    elif pending_review:
        verdict = "pending"
    blockers = await conn.fetchrow(
        """SELECT count(*) FILTER (
                      WHERE COALESCE(d.dependency_class,'execution')='execution'
                        AND dep.status<>'completed') AS dependencies,
                  count(*) FILTER (
                      WHERE COALESCE(d.dependency_class,'execution')<>'execution'
                        AND dep.status<>'completed') AS blockers
             FROM work_item_dependencies d
             JOIN work_items dep ON dep.id=d.depends_on_id AND dep.tenant_id=d.tenant_id
                              AND dep.project=d.project AND dep.goal_id=d.goal_id
            WHERE d.tenant_id=$1::uuid AND d.project=$2 AND d.work_item_id=$3::uuid""",
        tenant_id, project, item_id,
    )
    snapshot = {
        "parent_state": str(parent["status"]),
        "parent_version": parent_version,
        "evidence_complete": evidence_complete,
        "evidence_snapshot_hash": evidence_hash,
        "review_verdict": verdict,
        "blocker_count": int(blockers["blockers"] or 0),
        "dependency_blocker_count": int(blockers["dependencies"] or 0),
    }
    snapshot_hash = _canonical_hash(snapshot)
    version = await conn.fetchval(
        """SELECT COALESCE(max(snapshot_version),0)+1 FROM goal_precondition_snapshots
            WHERE tenant_id=$1::uuid AND work_item_id=$2::uuid""",
        tenant_id, item_id,
    )
    if persist:
        prior = await conn.fetchrow(
            """SELECT snapshot_version,snapshot_hash FROM goal_precondition_snapshots
                WHERE tenant_id=$1::uuid AND work_item_id=$2::uuid
                ORDER BY snapshot_version DESC LIMIT 1""", tenant_id, item_id,
        )
        if prior and prior["snapshot_hash"] == snapshot_hash:
            version = int(prior["snapshot_version"])
        else:
            await conn.execute(
                """INSERT INTO goal_precondition_snapshots
                    (tenant_id,project,goal_id,work_item_id,snapshot_version,target_version,
                     assignment_id,snapshot,snapshot_hash,evidence_snapshot_hash,computed_by)
                    VALUES($1::uuid,$2,$3::uuid,$4::uuid,$5,$6,$7::uuid,$8::jsonb,$9,$10,$11::uuid)""",
                tenant_id, project, item["goal_id"], item_id, version, item["version"],
                assignment["id"], json.dumps(snapshot), snapshot_hash,
                evidence_hash, actor.session_id,
            )
    return {
        "preconditions": snapshot, "precondition_snapshot_hash": snapshot_hash,
        "snapshot_version": int(version), "target_version": int(item["version"]),
        "target_type": str(item["type"]), "tenant_id": tenant_id, "project": project,
        "assignment_id": assignment["id"],
        "principal_session_id": assignment["session_id"],
    }


async def create_policy_input(
    conn: Any, *, tenant_id: str, item_id: str, actor: ActorScope, payload: Mapping[str, Any]
) -> dict[str, Any]:
    computed = await compute_preconditions(
        conn, tenant_id=tenant_id, item_id=item_id, actor=actor,
        assignment_id=str(payload["assignment_id"]) if payload.get("assignment_id") else None,
        expected_parent_version=payload.get("expected_parent_version"), persist=True,
    )
    if int(payload["base_version"]) != computed["target_version"]:
        raise _error(409, "version_conflict")
    patch_hash = _canonical_hash({"patch": payload.get("patch") or []})
    policy = await conn.fetchrow(
        """SELECT id::text FROM goal_approval_policy_versions
            WHERE tenant_id=$1::uuid AND (project=$2 OR project IS NULL)
            ORDER BY (project=$2) DESC,effective_at DESC NULLS LAST,created_at DESC LIMIT 1""",
        tenant_id, computed["project"],
    )
    policy_version = policy["id"] if policy else None
    row = await conn.fetchrow(
        """INSERT INTO goal_policy_inputs
            (tenant_id,project,workspace_kind,work_item_id,target_type,target_version,
             assignment_id,principal_session_id,coordinator_session_id,
             action,patch,patch_hash,environment,risk_factors,
             precondition_snapshot_version,precondition_snapshot_hash,policy_version)
            VALUES($1::uuid,$2,$3,$4::uuid,$5,$6,$7::uuid,$8::uuid,$9::uuid,
                   $10,$11::jsonb,$12,$13,$14::text[],$15,$16,$17::uuid)
            RETURNING id::text,created_at""",
        tenant_id, computed["project"],
        "ceo" if actor.workspace_kind == "ceo_integrated" else "project",
        item_id, computed["target_type"], computed["target_version"], computed["assignment_id"],
        computed["principal_session_id"], actor.session_id,
        payload["action"], json.dumps(payload.get("patch") or []), patch_hash,
        payload["environment"], payload.get("risk_factors") or [],
        computed["snapshot_version"], computed["precondition_snapshot_hash"], policy_version,
    )
    return {
        "policy_input_id": row["id"], "decision_id": None, "version": computed["target_version"],
        "tenant_id": tenant_id, "project": computed["project"], "policy_version": policy_version,
        "matched_grant_id": None, "grant_version": None, "remaining_uses": None,
        "reason_codes": ["evaluation_required"], "last_error": None,
        "pending_approval_count": 0, "automation_claimed": False, **computed,
        "patch_hash": patch_hash,
    }


async def require_current_preconditions(
    conn: Any, *, tenant_id: str, item_id: str, actor: ActorScope,
    expected_snapshot_hash: str, assignment_id: str | None = None,
) -> dict[str, Any]:
    """Execution-boundary guard: stale decisions fail before any grant operation."""
    current = await compute_preconditions(
        conn, tenant_id=tenant_id, item_id=item_id, actor=actor,
        assignment_id=assignment_id, persist=True,
    )
    if current["precondition_snapshot_hash"] != expected_snapshot_hash:
        raise _error(422, "precondition_unmet")
    return current
