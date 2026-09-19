from __future__ import annotations

import asyncio
import inspect

import pytest
from fastapi import HTTPException
from fastapi.params import Depends

from app.routers import goals


TENANT_ENDPOINTS = (
    "goal_board",
    "goal_documents",
    "add_goal_document",
    "goals_for_session",
    "goal_owner_candidates",
    "add_goal_owner",
    "get_goal_approval_policy",
    "set_goal_approval_policy",
    "set_goal_lead",
    "remove_goal_owner",
    "pause_owner",
    "resume_owner",
    "restart_owner",
    "goal_halt",
    "goal_direct",
    "confirm_milestone_api",
    "goal_rewind",
    "advance_active_goals",
    "update_task_status",
    "reconcile_goal_links",
    "reconcile_release_evidence",
)


@pytest.mark.parametrize("endpoint_name", TENANT_ENDPOINTS)
def test_all_21_tenant_endpoints_require_tenant_context(endpoint_name):
    endpoint = getattr(goals, endpoint_name)
    parameter = inspect.signature(endpoint).parameters.get("context")

    assert parameter is not None, f"{endpoint_name} has no tenant context"
    assert isinstance(parameter.default, Depends)
    assert parameter.default.dependency in {
        goals.require_tenant_viewer,
        goals.require_tenant_member,
    }


def test_tenant_helper_rejects_direct_call_depends_object():
    with pytest.raises(HTTPException) as exc:
        goals._tenant_id(inspect.signature(goals.restart_owner).parameters["context"].default)

    assert exc.value.status_code == 401
    assert exc.value.detail == "tenant_context_required"


def test_restart_owner_direct_call_without_context_fails_closed():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.restart_owner("goal-id", "session-id"))

    assert exc.value.status_code == 401
    assert exc.value.detail == "tenant_context_required"


# The dependency check above protects route wiring.  These tests exercise the
# route bodies as well: a tiny DB double holds a single tenant-A goal and
# refuses every tenant-B lookup before a write can be made.  This is deliberate
# rather than a SQL-string test: it proves the authenticated context reaches
# the resource lookup/mutation boundary for every legacy tenant endpoint.
TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GOAL_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
MILESTONE_ID = "33333333-3333-4333-8333-333333333333"


def _context(tenant_id, *, admin=False):
    return {"tenant": {"id": tenant_id}, "user": {"user_id": "ceo", "is_internal_admin": admin}}


class _TenantGoalDB:
    """Minimal stateful DB boundary: only tenant A owns the fixture rows."""

    def __init__(self):
        self.mutations = 0
        self.tenant_args = []

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _owned(self, query, args):
        if "tenant_id" not in query:
            return True
        tenants = [str(arg) for arg in args if str(arg) in {TENANT_A, TENANT_B}]
        self.tenant_args.extend(tenants)
        return not tenants or tenants[-1] == TENANT_A

    @staticmethod
    def _write(query):
        return query.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "WITH"))

    async def fetchval(self, query, *args):
        if self._write(query):
            if self._owned(query, args):
                self.mutations += 1
                return GOAL_ID
            return None
        if not self._owned(query, args):
            return None
        if "COALESCE(m.owner_session_id" in query:
            return None
        return True

    async def fetchrow(self, query, *args):
        if not self._owned(query, args):
            return None
        if self._write(query):
            self.mutations += 1
        if "FROM chat_sessions" in query:
            return {"id": SESSION_ID, "title": "Tenant A session", "role_key": "lead",
                    "project_key": "AADS", "workspace": "A"}
        if "goal_documents" in query:
            return {"kind": "reference", "doc_path": "docs/a.md", "title": None}
        if "UPDATE goals" in query:
            return {"id": GOAL_ID, "title": "Tenant A goal"}
        return {"id": GOAL_ID, "project": "AADS", "title": "Tenant A goal", "status": "active",
                "progress": 0, "owner_role_key": "lead", "owner_session_id": SESSION_ID, "p": {}}

    async def fetch(self, query, *args):
        if not self._owned(query, args):
            return []
        return []

    async def execute(self, query, *args):
        if self._owned(query, args):
            self.mutations += 1
        return "OK"


class _GoalMachine:
    def __init__(self, writes):
        self.writes = writes

    async def advance_active_goals(self, project, *, tenant_id):
        if tenant_id == TENANT_A:
            self.writes.append("advance")
        return {"tenant_id": tenant_id}

    async def update_task_status_with_phase(self, *, tenant_id, **_kwargs):
        if tenant_id == TENANT_A:
            self.writes.append("task_status")
        return {"tenant_id": tenant_id}


def _install_tenant_doubles(monkeypatch, db, service_writes):
    """Patch only I/O seams; route code and its real argument wiring remain live."""
    from app.core import db_pool
    from app.services import (
        direction_guard, goal_intervene, goal_link_reconciler, goal_manager,
        milestone_review, ohvis_alert, release_evidence,
    )

    monkeypatch.setattr(db_pool, "get_pool", lambda: db)
    monkeypatch.setattr(goal_manager, "goal_state_machine", _GoalMachine(service_writes))

    async def tenant_result(*_args, tenant_id=None, **_kwargs):
        if tenant_id == TENANT_A:
            service_writes.append("service")
            return {"ok": True, "milestone": "Tenant A milestone"}
        return {"error": "goal_not_found"}

    async def tenant_collection(*_args, tenant_id=None, **_kwargs):
        if tenant_id == TENANT_A:
            service_writes.append("collection")
        return {"tenant_id": tenant_id}

    async def milestone_result(*_args, tenant_id=None, **_kwargs):
        if tenant_id == TENANT_A:
            service_writes.append("service")
            return {"ok": True, "milestone": "Tenant A milestone"}
        return {"error": "milestone_not_found"}

    monkeypatch.setattr(goal_intervene, "direct", tenant_result)
    monkeypatch.setattr(goal_intervene, "rewind", tenant_result)
    monkeypatch.setattr(milestone_review, "confirm", milestone_result)
    monkeypatch.setattr(goal_link_reconciler, "reconcile", tenant_collection)
    monkeypatch.setattr(release_evidence, "reconcile_release_links", tenant_collection)

    async def is_halted():
        return False

    async def notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(direction_guard, "is_halted", is_halted)
    monkeypatch.setattr(ohvis_alert, "notify", notify)


RESOURCE_ENDPOINTS = (
    ("goal_board", lambda c: goals.goal_board(GOAL_ID, c), False),
    ("goal_documents", lambda c: goals.goal_documents(GOAL_ID, c), False),
    ("add_goal_document", lambda c: goals.add_goal_document(GOAL_ID, goals.GoalDocRequest(doc_path="docs/a.md"), c), True),
    ("goals_for_session", lambda c: goals.goals_for_session(SESSION_ID, c), False),
    ("goal_owner_candidates", lambda c: goals.goal_owner_candidates(GOAL_ID, c), False),
    ("add_goal_owner", lambda c: goals.add_goal_owner(GOAL_ID, goals.AddOwnerRequest(session_id=SESSION_ID), c), True),
    ("get_goal_approval_policy", lambda c: goals.get_goal_approval_policy(GOAL_ID, c), False),
    ("set_goal_approval_policy", lambda c: goals.set_goal_approval_policy(GOAL_ID, goals.GoalApprovalPolicyRequest(), c), True),
    ("set_goal_lead", lambda c: goals.set_goal_lead(GOAL_ID, goals.GoalLeadRequest(session_id=SESSION_ID), c), True),
    ("remove_goal_owner", lambda c: goals.remove_goal_owner(GOAL_ID, SESSION_ID, c), True),
    ("pause_owner", lambda c: goals.pause_owner(GOAL_ID, SESSION_ID, goals.InterveneRequest(reason="test"), c), True),
    ("resume_owner", lambda c: goals.resume_owner(GOAL_ID, SESSION_ID, c), True),
    ("restart_owner", lambda c: goals.restart_owner(GOAL_ID, SESSION_ID, c), True),
    ("goal_direct", lambda c: goals.goal_direct(GOAL_ID, goals.InterveneRequest(message="test"), c), True),
    ("confirm_milestone_api", lambda c: goals.confirm_milestone_api(MILESTONE_ID, goals.ConfirmRequest(ok=True), c), True),
    ("goal_rewind", lambda c: goals.goal_rewind(MILESTONE_ID, goals.InterveneRequest(reason="test"), c), True),
)


@pytest.mark.parametrize("endpoint_name,invoke,mutates", RESOURCE_ENDPOINTS, ids=[row[0] for row in RESOURCE_ENDPOINTS])
def test_resource_endpoints_allow_owner_and_block_foreign_tenant(monkeypatch, endpoint_name, invoke, mutates):
    """Tenant A may use its resource; tenant B gets 404 before any row changes."""
    db = _TenantGoalDB()
    service_writes = []
    _install_tenant_doubles(monkeypatch, db, service_writes)

    asyncio.run(invoke(_context(TENANT_A)))
    if mutates:
        assert db.mutations or service_writes, endpoint_name

    before = (db.mutations, list(service_writes))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(invoke(_context(TENANT_B)))
    assert exc.value.status_code == 404
    assert (db.mutations, service_writes) == before


COLLECTION_ENDPOINTS = (
    ("advance_active_goals", lambda c: goals.advance_active_goals(context=c)),
    ("update_task_status", lambda c: goals.update_task_status(goals.TaskStatusRequest(task_type="job", task_id="job-a", status="done"), c)),
    ("reconcile_goal_links", lambda c: goals.reconcile_goal_links(context=c)),
    ("reconcile_release_evidence", lambda c: goals.reconcile_release_evidence(context=c)),
)


@pytest.mark.parametrize("endpoint_name,invoke", COLLECTION_ENDPOINTS, ids=[row[0] for row in COLLECTION_ENDPOINTS])
def test_collection_endpoints_are_tenant_scoped_without_cross_tenant_mutation(monkeypatch, endpoint_name, invoke):
    """Collection routes have no foreign ID: tenant B is separately scoped, never A."""
    db = _TenantGoalDB()
    service_writes = []
    _install_tenant_doubles(monkeypatch, db, service_writes)

    assert asyncio.run(invoke(_context(TENANT_A)))["tenant_id"] == TENANT_A
    writes_after_a = list(service_writes)
    assert asyncio.run(invoke(_context(TENANT_B)))["tenant_id"] == TENANT_B
    assert service_writes == writes_after_a
    assert db.mutations == 0


def test_goal_halt_separates_non_admin_denial_from_admin_policy(monkeypatch):
    """Halt is global, so it is admin-gated rather than faking a tenant resource."""
    from app.services import goal_intervene

    calls = []

    async def halt(on, reason):
        calls.append((on, reason))
        return {"halted": on}

    monkeypatch.setattr(goal_intervene, "halt", halt)
    request = goals.InterveneRequest(on=True, reason="policy test")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.goal_halt(request, _context(TENANT_A)))
    assert exc.value.status_code == 403
    assert calls == []
    assert asyncio.run(goals.goal_halt(request, _context(TENANT_A, admin=True))) == {"halted": True}
    assert calls == [(True, "policy test")]
