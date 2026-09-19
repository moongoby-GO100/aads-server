from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.routers.work_items import PolicyInputRequest
from app.services.goal_policy_preconditions import (
    _canonical_hash,
    _local_assignment,
    compute_preconditions,
)
from app.services.goal_work_hierarchy import ActorScope


TENANT = "00000000-0000-0000-0000-000000000001"
CEO_SESSION = "00000000-0000-0000-0000-000000000002"
LOCAL_SESSION = "00000000-0000-0000-0000-000000000003"
ASSIGNMENT = "00000000-0000-0000-0000-000000000004"


class AssignmentConnection:
    def __init__(self, row):
        self.row = row
        self.query = ""
        self.args = ()

    async def fetchrow(self, sql, *args):
        self.query = sql
        self.args = args
        return self.row


class PreconditionsConnection:
    def __init__(self, *, criteria, evidence):
        self.criteria = criteria
        self.evidence = evidence
        self.evidence_query = ""

    async def fetchrow(self, sql, *args):
        if "FROM work_items WHERE" in sql:
            return {
                "id": ASSIGNMENT,
                "tenant_id": TENANT,
                "project": "AADS",
                "goal_id": ASSIGNMENT,
                "milestone_id": ASSIGNMENT,
                "parent_id": None,
                "type": "task",
                "status": "in_review",
                "version": 3,
                "acceptance_criteria": self.criteria,
                "assignment_id": ASSIGNMENT,
            }
        if "FROM project_role_assignments" in sql:
            return {"id": ASSIGNMENT, "session_id": LOCAL_SESSION, "role_key": "goal_owner"}
        if "FROM milestones" in sql:
            return {"status": "active", "version": 1}
        if "FROM work_item_review_decisions" in sql:
            return None
        if "FROM work_item_dependencies" in sql:
            return {"dependencies": 0, "blockers": 0}
        if "FROM goal_precondition_snapshots" in sql:
            return None
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        self.evidence_query = sql
        return self.evidence

    async def fetchval(self, sql, *args):
        if "work_item_review_requirements" in sql:
            return False
        if "max(snapshot_version)" in sql:
            return 1
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        return "OK"


def _actor(session_id: str, kind: str, project: str) -> ActorScope:
    return ActorScope(TENANT, session_id, "goal_owner", project, kind)


def test_t56_client_precondition_object_is_not_a_policy_input_field():
    request = PolicyInputRequest.model_validate({
        "action": "execute",
        "base_version": 8,
        "expected_parent_version": 7,
        "preconditions": {"evidence_complete": True, "blocker_count": 0},
    })

    dumped = request.model_dump()
    assert dumped["expected_parent_version"] == 7
    assert "preconditions" not in dumped


def test_t57_policy_input_does_not_offer_cross_project_override():
    fields = PolicyInputRequest.model_fields

    assert "target_project" not in fields
    assert "allow_cross_project" not in fields
    assert "explicit_approval_id" not in fields


def test_evidence_snapshot_hash_is_deterministic_for_object_key_order():
    first = {"version": 3, "evidence": [{"state": "verified", "artifact_hash": "sha256:a"}]}
    second = {"evidence": [{"artifact_hash": "sha256:a", "state": "verified"}], "version": 3}

    assert _canonical_hash(first) == _canonical_hash(second)


def test_evidence_snapshot_hash_changes_when_version_changes():
    assert _canonical_hash({"version": 3, "evidence": []}) != _canonical_hash(
        {"version": 4, "evidence": []}
    )


def test_project_actor_cannot_select_another_sessions_assignment():
    conn = AssignmentConnection(None)
    actor = _actor(LOCAL_SESSION, "project", "AADS")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_local_assignment(
            conn, tenant_id=TENANT, project="AADS", actor=actor,
            assignment_id=ASSIGNMENT,
        ))

    assert exc.value.detail["code"] == "project_scope_denied"
    assert "session_id=$3::uuid" in conn.query
    assert conn.args == (TENANT, "AADS", LOCAL_SESSION, ASSIGNMENT)


def test_ceo_coordinator_resolves_execution_principal_from_local_assignment():
    row = {"id": ASSIGNMENT, "session_id": LOCAL_SESSION, "role_key": "goal_owner"}
    conn = AssignmentConnection(row)
    actor = _actor(CEO_SESSION, "ceo_integrated", "CEO")

    resolved = asyncio.run(_local_assignment(
        conn, tenant_id=TENANT, project="AADS", actor=actor,
        assignment_id=ASSIGNMENT,
    ))

    assert resolved["session_id"] == LOCAL_SESSION
    assert "id=$3::uuid" in conn.query
    assert "session_id=" not in conn.query
    assert conn.args == (TENANT, "AADS", ASSIGNMENT)


def test_ceo_coordinator_requires_explicit_local_assignment():
    actor = _actor(CEO_SESSION, "ceo_integrated", "CEO")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_local_assignment(
            AssignmentConnection(None), tenant_id=TENANT, project="AADS",
            actor=actor, assignment_id=None,
        ))
    assert exc.value.detail["code"] == "local_assignment_required"


def test_string_acceptance_criteria_require_one_verified_key_each():
    conn = PreconditionsConnection(
        criteria=["first", "second"],
        evidence=[{
            "id": "evidence-1", "criterion_key": "0",
            "artifact_hash": "sha256:first", "state": "verified",
        }],
    )

    result = asyncio.run(compute_preconditions(
        conn, tenant_id=TENANT, item_id=ASSIGNMENT,
        actor=_actor(LOCAL_SESSION, "project", "AADS"), persist=False,
    ))

    assert result["preconditions"]["evidence_complete"] is False
    assert "work_item_version=$4" in conn.evidence_query
    assert "COALESCE(work_item_version" not in conn.evidence_query


def test_string_acceptance_criteria_complete_only_when_all_keys_are_verified():
    conn = PreconditionsConnection(
        criteria=["first", "second"],
        evidence=[
            {"id": "evidence-1", "criterion_key": "0", "artifact_hash": "sha256:first", "state": "verified"},
            {"id": "evidence-2", "criterion_key": "1", "artifact_hash": "sha256:second", "state": "verified"},
        ],
    )

    result = asyncio.run(compute_preconditions(
        conn, tenant_id=TENANT, item_id=ASSIGNMENT,
        actor=_actor(LOCAL_SESSION, "project", "AADS"), persist=False,
    ))

    assert result["preconditions"]["evidence_complete"] is True


def test_epic_parent_precondition_uses_milestone_version():
    conn = PreconditionsConnection(criteria=[], evidence=[])

    result = asyncio.run(compute_preconditions(
        conn, tenant_id=TENANT, item_id=ASSIGNMENT,
        actor=_actor(LOCAL_SESSION, "project", "AADS"), persist=False,
    ))

    assert result["preconditions"]["parent_state"] == "active"
    assert result["preconditions"]["parent_version"] == 1
