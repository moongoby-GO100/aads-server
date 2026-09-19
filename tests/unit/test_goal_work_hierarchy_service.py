from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.services.goal_work_hierarchy import (
    ActorScope,
    create_project_assignment,
    create_work_item,
    require_project_access,
)


TENANT = "00000000-0000-0000-0000-000000000001"
GOAL = "00000000-0000-0000-0000-000000000002"
MILESTONE = "00000000-0000-0000-0000-000000000003"
SESSION = "00000000-0000-0000-0000-000000000004"
PARENT = "00000000-0000-0000-0000-000000000005"


def _actor(project: str = "AADS", kind: str = "project") -> ActorScope:
    return ActorScope(TENANT, SESSION, "lead", project, kind)


class WorkItemConnection:
    def __init__(self, parent_type: str = "task", *, parent_visible: bool = True):
        self.parent_type = parent_type
        self.parent_visible = parent_visible

    async def fetchrow(self, sql, *args):
        if "FROM goals" in sql:
            return {"id": GOAL, "tenant_id": TENANT, "project": "AADS", "title": "G", "status": "active", "version": 1}
        if "FROM milestones" in sql:
            return {"id": MILESTONE}
        if "FROM work_items" in sql and "idempotency_key" not in sql:
            if not self.parent_visible:
                return None
            return {"id": PARENT, "type": self.parent_type, "milestone_id": MILESTONE}
        return None

    async def fetchval(self, sql, *args):
        return True

    async def execute(self, sql, *args):
        return "SELECT 1"


def test_t01_task_rejects_non_story_parent():
    payload = {
        "type": "task", "milestone_id": MILESTONE, "parent_id": PARENT,
        "title": "T", "idempotency_key": "t01",
    }
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_work_item(
            WorkItemConnection(parent_type="task"), tenant_id=TENANT,
            actor=_actor(), goal_id=GOAL, payload=payload,
        ))
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "invalid_parent"


def test_t02_foreign_tenant_parent_is_not_exposed():
    payload = {
        "type": "task", "milestone_id": MILESTONE, "parent_id": PARENT,
        "title": "T", "idempotency_key": "t02",
    }
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_work_item(
            WorkItemConnection(parent_type="story", parent_visible=False),
            tenant_id=TENANT, actor=_actor(), goal_id=GOAL, payload=payload,
        ))
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "parent_not_found"


class ExistingRootKeyConnection(WorkItemConnection):
    async def fetchrow(self, sql, *args):
        if "idempotency_key" in sql:
            return {
                "id": "00000000-0000-0000-0000-000000000099",
                "goal_id": "00000000-0000-0000-0000-000000000098",
                "milestone_id": MILESTONE,
                "parent_id": None,
                "type": "epic",
                "title": "E",
                "description": None,
                "acceptance_criteria": [],
                "priority": "P2",
                "assignment_id": None,
            }
        return await super().fetchrow(sql, *args)


def test_root_idempotency_key_cannot_replay_another_goal_resource():
    payload = {
        "type": "epic", "milestone_id": MILESTONE, "parent_id": None,
        "title": "E", "idempotency_key": "shared-root-key",
    }
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_work_item(
            ExistingRootKeyConnection(), tenant_id=TENANT,
            actor=_actor(), goal_id=GOAL, payload=payload,
        ))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "idempotency_conflict"


def test_t04_regular_project_cannot_manage_other_project():
    with pytest.raises(HTTPException) as exc:
        require_project_access(_actor("AADS"), "GO100")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "project_scope_denied"


def test_t05_ceo_integrated_can_coordinate_other_project():
    require_project_access(_actor("CEO", "ceo_integrated"), "GO100")


class DuplicateAssignmentConnection:
    async def fetchrow(self, sql, *args):
        if "FROM goals" in sql:
            return {"id": GOAL, "tenant_id": TENANT, "project": "AADS", "title": "G", "status": "active", "version": 1}
        if "FROM chat_sessions" in sql:
            return {"id": SESSION, "project": "AADS"}
        error = RuntimeError("duplicate")
        error.sqlstate = "23505"
        raise error


def test_t03_unique_constraint_is_mapped_to_duplicate_assignment():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_project_assignment(
            DuplicateAssignmentConnection(), tenant_id=TENANT, actor=_actor(),
            goal_id=GOAL, role_key="lead", session_id=SESSION,
        ))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "duplicate_assignment"
