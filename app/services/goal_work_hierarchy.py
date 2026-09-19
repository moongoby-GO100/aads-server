"""M13 work hierarchy CRUD and project-boundary enforcement.

This module deliberately does not create change sets, approval outbox rows, or
auto-approval grants.  Those execution concerns belong to M14.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.core.goal_work_hierarchy_policy import PARENT_TYPE, WorkItemType


def _error(status: int, code: str, message: str | None = None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": message or code},
    )


def _row_dict(row: Any) -> dict[str, Any]:
    value = dict(row)
    for key, item in tuple(value.items()):
        if hasattr(item, "isoformat"):
            value[key] = item.isoformat()
        elif key in {"acceptance_criteria", "payload", "patch"} and isinstance(item, str):
            try:
                value[key] = json.loads(item)
            except json.JSONDecodeError:
                pass
        elif isinstance(item, UUID):
            value[key] = str(item)
    return value


@dataclass(frozen=True)
class ActorScope:
    tenant_id: str
    session_id: str
    role_key: str
    project: str
    workspace_kind: str

    def may_access(self, target_project: str) -> bool:
        return self.workspace_kind == "ceo_integrated" or self.project.upper() == target_project.upper()


async def resolve_actor_scope(
    conn: Any,
    *,
    tenant_id: str,
    user_id: str,
    actor_session_id: str | None,
    internal_admin: bool = False,
) -> ActorScope:
    """Resolve actor claims from DB source-of-truth, never request project/role."""
    if not actor_session_id:
        raise _error(403, "project_scope_denied", "X-Chat-Session-ID is required")
    row = await conn.fetchrow(
        """
        SELECT s.id::text AS session_id,
               COALESCE(s.role_key, '') AS role_key,
               COALESCE(w.project_key, '') AS project,
               COALESCE(w.name, '') AS workspace_name,
               COALESCE(w.display_name, '') AS display_name,
               s.user_id
          FROM chat_sessions s
          JOIN chat_workspaces w
            ON w.id = s.workspace_id AND w.tenant_id = s.tenant_id
         WHERE s.id = $1::uuid AND s.tenant_id = $2::uuid
        """,
        actor_session_id,
        tenant_id,
    )
    if not row:
        raise _error(403, "project_scope_denied")
    # Service/admin principals can operate sessions for internal automation;
    # normal users may only assert a session attributed to their login.
    if not internal_admin and str(row["user_id"] or "") != str(user_id):
        raise _error(403, "project_scope_denied")
    name = str(row["workspace_name"] or "").strip()
    display_name = str(row["display_name"] or "").strip()
    project = str(row["project"] or "").strip()
    integrated = project.upper() == "CEO" and "[CEO] 통합지시" in {name, display_name}
    return ActorScope(
        tenant_id=tenant_id,
        session_id=str(row["session_id"]),
        role_key=str(row["role_key"] or ""),
        project=project,
        workspace_kind="ceo_integrated" if integrated else "project",
    )


async def get_goal_scope(conn: Any, *, tenant_id: str, goal_id: str) -> Any:
    row = await conn.fetchrow(
        """SELECT id::text, tenant_id::text, project, title, status,
                  1::bigint AS version
             FROM goals
            WHERE id = $1::uuid AND tenant_id = $2::uuid""",
        goal_id,
        tenant_id,
    )
    if not row:
        raise _error(404, "goal_not_found")
    return row


def require_project_access(actor: ActorScope, target_project: str) -> None:
    if not actor.may_access(target_project):
        raise _error(403, "project_scope_denied")


async def create_work_item(
    conn: Any,
    *,
    tenant_id: str,
    actor: ActorScope,
    goal_id: str,
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    goal = await get_goal_scope(conn, tenant_id=tenant_id, goal_id=goal_id)
    project = str(goal["project"])
    require_project_access(actor, project)
    try:
        item_type = WorkItemType(str(payload["type"]))
    except (KeyError, ValueError):
        raise _error(422, "invalid_parent", "type must be epic, story, or task")
    parent_id = payload.get("parent_id")
    expected_parent = PARENT_TYPE[item_type]
    if (expected_parent is None) != (parent_id is None):
        raise _error(422, "invalid_parent")

    milestone = await conn.fetchrow(
        """SELECT id::text FROM milestones
            WHERE id=$1::uuid AND goal_id=$2::uuid
              AND tenant_id=$3::uuid AND project=$4""",
        payload["milestone_id"], goal_id, tenant_id, project,
    )
    if not milestone:
        raise _error(404, "milestone_not_found")
    if parent_id:
        parent = await conn.fetchrow(
            """SELECT id::text, type, milestone_id::text FROM work_items
                WHERE id=$1::uuid AND tenant_id=$2::uuid AND project=$3
                  AND goal_id=$4::uuid""",
            parent_id, tenant_id, project, goal_id,
        )
        if not parent:
            raise _error(404, "parent_not_found")
        if parent["type"] != expected_parent.value or str(parent["milestone_id"]) != str(payload["milestone_id"]):
            raise _error(422, "invalid_parent")

    assignment_id = payload.get("assignment_id")
    if assignment_id:
        exists = await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM project_role_assignments
                 WHERE id=$1::uuid AND tenant_id=$2::uuid AND project=$3 AND active)""",
            assignment_id, tenant_id, project,
        )
        if not exists:
            raise _error(403, "project_scope_denied", "assignment is not active in target project")

    # The DB uniqueness contract intentionally scopes root Epic keys to the
    # project (NULL parent), not the goal.  Lock that exact identity so two
    # goals cannot race past the read and turn a deterministic 409 into a 500.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"{tenant_id}:{project}:{parent_id or '<root>'}:{payload['idempotency_key']}",
    )
    existing = await conn.fetchrow(
        """SELECT * FROM work_items
            WHERE tenant_id=$1::uuid AND project=$2
              AND parent_id IS NOT DISTINCT FROM $3::uuid
              AND idempotency_key=$4""",
        tenant_id, project, parent_id, payload["idempotency_key"],
    )
    comparable = {
        "goal_id": str(goal_id),
        "type": item_type.value,
        "milestone_id": str(payload["milestone_id"]),
        "parent_id": str(parent_id) if parent_id else None,
        "title": str(payload["title"]),
        "description": payload.get("description"),
        "acceptance_criteria": payload.get("acceptance_criteria") or [],
        "priority": payload.get("priority") or "P2",
        "assignment_id": str(assignment_id) if assignment_id else None,
    }
    if existing:
        stored = _row_dict(existing)
        if any(stored.get(key) != value for key, value in comparable.items()):
            raise _error(409, "idempotency_conflict")
        return stored, False

    row = await conn.fetchrow(
        """INSERT INTO work_items
               (tenant_id, project, goal_id, milestone_id, parent_id, type,
                title, description, acceptance_criteria, priority, assignment_id,
                idempotency_key, created_by)
             VALUES ($1::uuid,$2,$3::uuid,$4::uuid,$5::uuid,$6,$7,$8,$9::jsonb,
                     $10,$11::uuid,$12,$13::uuid)
             RETURNING *""",
        tenant_id, project, goal_id, payload["milestone_id"], parent_id,
        item_type.value, payload["title"], payload.get("description"),
        json.dumps(payload.get("acceptance_criteria") or []),
        payload.get("priority") or "P2", assignment_id,
        payload["idempotency_key"], actor.session_id,
    )
    return _row_dict(row), True


async def create_project_assignment(
    conn: Any,
    *,
    tenant_id: str,
    actor: ActorScope,
    goal_id: str,
    role_key: str,
    session_id: str,
) -> dict[str, Any]:
    goal = await get_goal_scope(conn, tenant_id=tenant_id, goal_id=goal_id)
    project = str(goal["project"])
    require_project_access(actor, project)
    target = await conn.fetchrow(
        """SELECT s.id::text, COALESCE(w.project_key, '') AS project
             FROM chat_sessions s
             JOIN chat_workspaces w ON w.id=s.workspace_id AND w.tenant_id=s.tenant_id
            WHERE s.id=$1::uuid AND s.tenant_id=$2::uuid""",
        session_id, tenant_id,
    )
    if not target or str(target["project"] or "").upper() != project.upper():
        raise _error(403, "project_scope_denied", "assignment session must belong to target project")
    if actor.workspace_kind == "ceo_integrated" and actor.session_id == session_id:
        raise _error(403, "project_scope_denied", "CEO session cannot replace a local assignment")
    try:
        row = await conn.fetchrow(
            """INSERT INTO project_role_assignments
                   (tenant_id,project,role_key,session_id,assigned_by)
                 VALUES($1::uuid,$2,$3,$4::uuid,$5::uuid)
                 RETURNING *""",
            tenant_id, project, role_key.strip(), session_id, actor.session_id,
        )
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == "23505":
            raise _error(409, "duplicate_assignment") from exc
        raise
    return _row_dict(row)


async def get_goal_tree(
    conn: Any,
    *,
    tenant_id: str,
    goal_id: str,
    includes: Sequence[str],
) -> dict[str, Any]:
    goal = await get_goal_scope(conn, tenant_id=tenant_id, goal_id=goal_id)
    project = str(goal["project"])
    rows = await conn.fetch(
        """SELECT w.*,m.title AS milestone_title,
                  m.sequence_order AS milestone_sequence
             FROM work_items w
             JOIN milestones m
               ON m.id=w.milestone_id AND m.goal_id=w.goal_id
              AND m.tenant_id=w.tenant_id AND m.project=w.project
            WHERE w.tenant_id=$1::uuid AND w.project=$2 AND w.goal_id=$3::uuid
            ORDER BY m.sequence_order,w.created_at,w.id""",
        tenant_id, project, goal_id,
    )
    items = {str(_row_dict(row)["id"]): _row_dict(row) for row in rows}
    for item in items.values():
        item["children"] = []
        item["last_error"] = None
        item["recovery"] = None
        item["pending_approval_count"] = 0
        if "evidence" in includes:
            item["evidence"] = {"count": 0, "verified_count": 0, "items": []}
        if "dependencies" in includes:
            item["dependencies"] = []
    if "evidence" in includes and items:
        summaries = await conn.fetch(
            """SELECT work_item_id::text AS id, count(*) AS count,
                      count(*) FILTER (WHERE verified) AS verified_count
                 FROM work_item_evidence
                WHERE tenant_id=$1::uuid AND project=$2 AND goal_id=$3::uuid
                GROUP BY work_item_id""",
            tenant_id, project, goal_id,
        )
        for row in summaries:
            items[row["id"]]["evidence"].update(
                {"count": row["count"], "verified_count": row["verified_count"]}
            )
        evidence_rows = await conn.fetch(
            """WITH ranked AS (
                 SELECT work_item_id::text AS item_id,id::text,evidence_type,uri,
                        criterion_key,verified,created_at,
                        row_number() OVER (
                            PARTITION BY work_item_id ORDER BY created_at DESC,id DESC
                        ) AS row_number
                   FROM work_item_evidence
                  WHERE tenant_id=$1::uuid AND project=$2 AND goal_id=$3::uuid
               )
               SELECT item_id,id,evidence_type,uri,criterion_key,verified,created_at
                 FROM ranked WHERE row_number<=5
                ORDER BY item_id,created_at DESC,id DESC""",
            tenant_id, project, goal_id,
        )
        for row in evidence_rows:
            item_id = str(row["item_id"])
            if item_id in items:
                items[item_id]["evidence"]["items"].append(_row_dict(row))
    if "dependencies" in includes and items:
        deps = await conn.fetch(
            """SELECT work_item_id::text, depends_on_id::text, dependency_type
                 FROM work_item_dependencies
                WHERE tenant_id=$1::uuid AND project=$2 AND goal_id=$3::uuid""",
            tenant_id, project, goal_id,
        )
        for row in deps:
            items[row["work_item_id"]].setdefault("dependencies", []).append(
                {"work_item_id": row["depends_on_id"], "type": row["dependency_type"]}
            )
    if "approvals" in includes and items:
        approvals = await conn.fetch(
            """SELECT target_id::text AS id, count(*) AS count
                 FROM work_item_change_sets
                WHERE tenant_id=$1::uuid AND project=$2 AND state='pending'
                  AND target_id=ANY($3::uuid[])
                GROUP BY target_id""",
            tenant_id, project, list(items),
        )
        for row in approvals:
            items[row["id"]]["pending_approval_count"] = row["count"]
    if items:
        recoveries = await conn.fetch(
            """SELECT DISTINCT ON (c.target_id)
                      c.target_id::text AS id,c.id::text AS change_set_id,
                      c.state AS change_set_state,o.id::text AS outbox_id,
                      o.status AS delivery_state,o.attempts,o.available_at,
                      COALESCE(o.last_error,c.last_error) AS last_error
                 FROM work_item_change_sets c
                 LEFT JOIN LATERAL (
                     SELECT id,status,attempts,available_at,last_error,created_at
                       FROM goal_workflow_outbox
                      WHERE tenant_id=c.tenant_id AND change_set_id=c.id
                      ORDER BY created_at DESC,id DESC LIMIT 1
                 ) o ON TRUE
                WHERE c.tenant_id=$1::uuid AND c.project=$2
                  AND c.target_id=ANY($3::uuid[])
                  AND (c.last_error IS NOT NULL
                       OR o.status IN ('failed','reconciliation_required'))
                ORDER BY c.target_id,c.created_at DESC,c.id DESC""",
            tenant_id, project, list(items),
        )
        for row in recoveries:
            item_id = str(row["id"])
            recovery = _row_dict(row)
            recovery["can_retry"] = recovery.get("delivery_state") == "failed"
            items[item_id]["last_error"] = recovery.get("last_error")
            items[item_id]["recovery"] = recovery
    roots: list[dict[str, Any]] = []
    for item in items.values():
        parent_id = item.get("parent_id")
        if parent_id and parent_id in items:
            items[parent_id]["children"].append(item)
        else:
            roots.append(item)
    return {
        "goal_id": str(goal["id"]), "tenant_id": tenant_id, "project": project,
        "version": goal["version"], "last_error": None,
        "pending_approval_count": sum(item["pending_approval_count"] for item in items.values()),
        "items": roots,
    }


async def get_governance(conn: Any, *, tenant_id: str, goal_id: str) -> dict[str, Any]:
    goal = await get_goal_scope(conn, tenant_id=tenant_id, goal_id=goal_id)
    project = str(goal["project"])
    assignments = await conn.fetch(
        """SELECT p.id::text, p.role_key, p.session_id::text, p.assigned_by::text,
                  p.assigned_at, s.title AS session_title
             FROM project_role_assignments p
             JOIN chat_sessions s ON s.id=p.session_id AND s.tenant_id=p.tenant_id
            WHERE p.tenant_id=$1::uuid AND p.project=$2 AND p.active
            ORDER BY p.role_key""",
        tenant_id, project,
    )
    policy = await conn.fetchrow(
        """SELECT id::text, policy_hash, mode, effective_at
             FROM goal_approval_policy_versions
            WHERE tenant_id=$1::uuid AND (project=$2 OR project IS NULL)
              AND mode IN ('canary','enabled')
            ORDER BY (project=$2) DESC, effective_at DESC NULLS LAST, created_at DESC
            LIMIT 1""",
        tenant_id, project,
    )
    return {
        "goal_id": str(goal["id"]), "tenant_id": tenant_id, "project": project,
        "version": goal["version"], "last_error": None, "pending_approval_count": 0,
        "assignments": [_row_dict(row) for row in assignments],
        "boundary": {
            "project_workspace": "may manage only its own project",
            "ceo_integrated": "may coordinate across projects; assignment must remain local",
            "source_of_truth": ["auth_context", "chat_sessions", "chat_workspaces"],
        },
        "policy": _row_dict(policy) if policy else {"mode": "unconfigured", "fail_closed": True},
    }
