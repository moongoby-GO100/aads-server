from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.routers import goals
from app.services import goal_manager

TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GOAL = "11111111-1111-4111-8111-111111111111"
MS = "33333333-3333-4333-8333-333333333333"
SESS = "22222222-2222-4222-8222-222222222222"
SESS_OTHER_PROJECT = "44444444-4444-4444-8444-444444444444"
SESS_UNLINKED = "55555555-5555-4555-8555-555555555555"
SESS_B = "66666666-6666-4666-8666-666666666666"


def _ctx(tenant=TENANT_A):
    return {"tenant": {"id": tenant}, "user": {"user_id": "ceo", "is_internal_admin": False}}


class FakeDB:
    """Stateful stand-in: rows live in dicts, queries are matched by the table they touch."""

    def __init__(self):
        self.goals = {GOAL: {"tenant": TENANT_A, "project": "AADS"}}
        self.sessions = {
            SESS: {"tenant": TENANT_A, "project_key": "AADS"},
            SESS_OTHER_PROJECT: {"tenant": TENANT_A, "project_key": "KIS"},
            SESS_UNLINKED: {"tenant": TENANT_A, "project_key": "AADS"},
            SESS_B: {"tenant": TENANT_B, "project_key": "AADS"},
        }
        self.links = {(GOAL, SESS), (GOAL, SESS_OTHER_PROJECT), (GOAL, SESS_B)}
        self.milestones = {
            MS: {
                "goal_id": GOAL, "tenant": TENANT_A, "owner_session_id": None,
                "owner_role_key": None, "version": 1, "updated_at": "t0",
                "dispatch_note": "담당 세션 없음", "dispatch_blocked_at": "blocked-t0",
            }
        }
        self.writes = 0

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, query, *args):
        if "FROM goals WHERE" in query:
            goal = self.goals.get(args[0])
            if goal and goal["tenant"] == args[1]:
                return {"id": args[0], "project": goal["project"]}
            return None
        if "FROM milestones" in query:
            ms = self.milestones.get(args[0])
            if ms and ms["goal_id"] == args[1] and ms["tenant"] == args[2]:
                return {k: ms[k] for k in ("owner_session_id", "owner_role_key", "version")}
            return None
        if "FROM chat_sessions" in query:
            sess = self.sessions.get(args[0])
            if sess and sess["tenant"] == args[1]:
                return {"id": args[0], "project_key": sess["project_key"]}
            return None
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "FROM goal_task_links" in query:
            goal_id, session_id, tenant = args
            return 1 if (goal_id, session_id) in self.links and self.goals[goal_id]["tenant"] == tenant else None
        if query.lstrip().startswith("UPDATE milestones"):
            mid, gid, tenant, session, role = args
            ms = self.milestones[mid]
            assert ms["goal_id"] == gid and ms["tenant"] == tenant
            assert "dispatch_note" not in query and "dispatch_blocked_at" not in query
            ms.update(owner_session_id=session, owner_role_key=role, version=ms["version"] + 1,
                      updated_at="t1")
            self.writes += 1
            return ms["version"]
        raise AssertionError(query)

    async def execute(self, query, *args):
        assert "INSERT INTO milestones" in query
        ms_id, goal_id, title, sequence, criteria, auto, owner_session, owner_role = args
        goal = self.goals[goal_id]
        self.milestones[ms_id] = {
            "goal_id": goal_id, "tenant": goal["tenant"], "owner_session_id": owner_session,
            "owner_role_key": owner_role, "version": 1, "title": title, "sequence": sequence,
        }
        return "INSERT 0 1"


@pytest.fixture
def db(monkeypatch):
    from app.core import db_pool

    fake = FakeDB()
    monkeypatch.setattr(db_pool, "get_pool", lambda: fake)

    async def no_trace(self, *args, **kwargs):
        return None

    monkeypatch.setattr(goal_manager.GoalStateMachine, "_trace", no_trace)
    return fake


def _patch(ctx=None, *, session=SESS, role=None, goal=GOAL, milestone=MS):
    req = goals.MilestoneOwnerRequest(owner_session_id=UUID(session), owner_role_key=role)
    return asyncio.run(goals.set_milestone_owner(UUID(goal), UUID(milestone), req, ctx or _ctx()))


def _create(ctx=None, **kwargs):
    req = goals.MilestoneCreateRequest(title="m", sequence=701, **kwargs)
    return asyncio.run(goals.add_milestone(GOAL, req, ctx or _ctx()))


def test_create_stores_owner_session_and_role(db):
    result = _create(owner_session_id=UUID(SESS), owner_role_key=" chat_lead ")

    row = db.milestones[result["milestone_id"]]
    assert row["owner_session_id"] == SESS
    assert row["owner_role_key"] == "chat_lead"
    assert result["owner_session_id"] == SESS


def test_create_without_owner_keeps_null_owner(db):
    result = _create()

    row = db.milestones[result["milestone_id"]]
    assert row["owner_session_id"] is None and row["owner_role_key"] is None


def test_create_rejects_unlinked_session_and_writes_nothing(db):
    before = set(db.milestones)
    with pytest.raises(HTTPException) as exc:
        _create(owner_session_id=UUID(SESS_UNLINKED))

    assert (exc.value.status_code, exc.value.detail) == (422, "owner_not_linked")
    assert set(db.milestones) == before


def test_create_rejects_foreign_tenant_goal(db):
    with pytest.raises(HTTPException) as exc:
        _create(_ctx(TENANT_B), owner_session_id=UUID(SESS_B))

    assert exc.value.status_code == 404
    assert set(db.milestones) == {MS}


def test_create_role_key_max_length():
    with pytest.raises(ValueError):
        goals.MilestoneCreateRequest(title="m", sequence=1, owner_role_key="x" * 101)


def test_goal_create_rejects_nested_milestone_owner(db):
    req = goals.GoalCreateRequest(
        project="AADS", title="g",
        milestones=[goals.MilestoneCreateRequest(
            title="m", sequence=1, owner_session_id=UUID(SESS))],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(goals.create_goal(req, _ctx()))

    assert (exc.value.status_code, exc.value.detail) == (
        422, "milestone_owner_requires_existing_goal")


def test_patch_sets_owner_and_bumps_version_without_touching_dispatch(db, caplog):
    with caplog.at_level(logging.INFO, logger=goals.logger.name):
        result = _patch(role="chat_lead")

    row = db.milestones[MS]
    assert result["changed"] is True
    assert (row["owner_session_id"], row["owner_role_key"]) == (SESS, "chat_lead")
    assert row["version"] == 2 and result["version"] == 2
    assert row["updated_at"] == "t1"
    assert row["dispatch_note"] == "담당 세션 없음"
    assert row["dispatch_blocked_at"] == "blocked-t0"
    assert any("milestone_owner_set" in r.getMessage() and MS in r.getMessage()
               for r in caplog.records)


def test_patch_without_role_keeps_existing_role(db):
    db.milestones[MS]["owner_role_key"] = "keeper"

    _patch()

    assert db.milestones[MS]["owner_role_key"] == "keeper"
    assert db.milestones[MS]["owner_session_id"] == SESS


def test_patch_same_value_is_noop(db, caplog):
    _patch(role="chat_lead")
    writes, version = db.writes, db.milestones[MS]["version"]

    with caplog.at_level(logging.INFO, logger=goals.logger.name):
        again = _patch(role="chat_lead")
        omitted_role = _patch()

    assert again["changed"] is False and omitted_role["changed"] is False
    assert db.writes == writes
    assert db.milestones[MS]["version"] == version == again["version"]
    assert not [r for r in caplog.records if "milestone_owner_set" in r.getMessage()]


def test_patch_foreign_tenant_is_404_and_writes_nothing(db):
    with pytest.raises(HTTPException) as exc:
        _patch(_ctx(TENANT_B), session=SESS_B)

    assert (exc.value.status_code, exc.value.detail) == (404, "goal_not_found")
    assert db.writes == 0 and db.milestones[MS]["owner_session_id"] is None


def test_patch_foreign_tenant_session_is_404(db):
    with pytest.raises(HTTPException) as exc:
        _patch(session=SESS_B)

    assert (exc.value.status_code, exc.value.detail) == (404, "session_not_found")
    assert db.writes == 0


def test_patch_milestone_of_another_goal_is_404(db):
    db.milestones[MS]["goal_id"] = "99999999-9999-4999-8999-999999999999"

    with pytest.raises(HTTPException) as exc:
        _patch()

    assert (exc.value.status_code, exc.value.detail) == (404, "milestone_not_found")
    assert db.writes == 0


def test_patch_cross_project_session_is_403(db):
    with pytest.raises(HTTPException) as exc:
        _patch(session=SESS_OTHER_PROJECT)

    assert (exc.value.status_code, exc.value.detail) == (403, "cross_project_owner_denied")
    assert db.writes == 0


def test_patch_unlinked_session_is_422_owner_not_linked(db):
    with pytest.raises(HTTPException) as exc:
        _patch(session=SESS_UNLINKED)

    assert (exc.value.status_code, exc.value.detail) == (422, "owner_not_linked")
    assert db.writes == 0 and db.milestones[MS]["owner_session_id"] is None


def test_patch_route_is_member_gated():
    import inspect

    default = inspect.signature(goals.set_milestone_owner).parameters["context"].default
    assert default.dependency is goals.require_tenant_member
    paths = {(tuple(sorted(r.methods)), r.path) for r in goals.router.routes}
    assert (("PATCH",), "/goals/{goal_id}/milestones/{milestone_id}/owner") in paths
