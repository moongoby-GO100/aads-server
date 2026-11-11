"""work_item_change_set_create / work_item_create 도구 — 권한 판정과 서비스 함수 위임 (fake conn, DB 없음)."""
from __future__ import annotations

import asyncio

import pytest

TENANT = "00000000-0000-0000-0000-000000000001"
SESSION = "00000000-0000-0000-0000-000000000003"
TARGET = "00000000-0000-0000-0000-000000000010"
GOAL = "00000000-0000-0000-0000-000000000020"
MILESTONE = "00000000-0000-0000-0000-000000000030"


class FakeConn:
    def __init__(self, *, assigned=True, lead=False, user_id="user-1", project="AADS", sess_project="AADS"):
        self.assigned, self.lead, self.user_id = assigned, lead, user_id
        self.project, self.sess_project = project, sess_project
        self.writes = []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def fetchrow(self, sql, *args):
        if "FROM goals g JOIN chat_sessions" in sql:
            return {"project": self.project}
        if "FROM work_items" in sql:
            return {"project": self.project, "goal_id": GOAL}
        if "FROM chat_sessions WHERE id" in sql:
            return {"tenant_id": TENANT, "user_id": self.user_id}
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetchval(self, sql, *args):
        if "SELECT tenant_id::text FROM chat_sessions" in sql:
            return TENANT
        if "FROM project_role_assignments" in sql:
            return self.assigned
        if "FROM goals WHERE" in sql:
            return self.lead
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql, *args):
        self.writes.append(sql)


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self.conn


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GOAL_WORK_HIERARCHY_ENABLED", "true")
    monkeypatch.setenv("GOAL_WORKFLOW_APPROVAL_ENABLED", "true")
    from app.services import goal_work_hierarchy as gwh
    from app.services import goal_workflow_approval as gwa

    async def fake_actor(conn, *, tenant_id, user_id, actor_session_id, internal_admin=False):
        return gwh.ActorScope(tenant_id=tenant_id, session_id=actor_session_id, role_key="CTO",
                              project=conn.sess_project, workspace_kind="project", user_id=user_id)

    calls = {"cs": [], "route": [], "item": []}

    async def fake_create_cs(conn, *, tenant_id, actor, target_id, payload):
        calls["cs"].append((target_id, payload))
        return {"id": "cs-1", "state": "pending", "patch_hash": "sha256:x"}

    async def fake_route(conn, *, tenant_id, change_set_id, actor, routes=None):
        calls["route"].append(change_set_id)
        return {"required_routes": ["project_lead"], "approval_request_ids": ["ap-1"]}

    async def fake_item(conn, *, tenant_id, actor, goal_id, payload):
        calls["item"].append((goal_id, payload))
        return {"id": "wi-1", "type": payload["type"], "status": "draft"}, True

    monkeypatch.setattr(gwh, "resolve_actor_scope", fake_actor)
    monkeypatch.setattr(gwh, "create_work_item", fake_item)
    monkeypatch.setattr(gwa, "create_change_set", fake_create_cs)
    monkeypatch.setattr(gwa, "route_change_set", fake_route)
    return monkeypatch, calls


def _run(name, conn, inp, mp, *, session=SESSION):
    from app.core import db_pool
    from app.services import tool_executor as te

    mp.setattr(db_pool, "get_pool", lambda: FakePool(conn))
    token = te.current_chat_session_id.set(session)
    try:
        return asyncio.run(getattr(te.ToolExecutor(), name)(inp))
    finally:
        te.current_chat_session_id.reset(token)


CS_INPUT = {
    "target_id": TARGET, "base_version": 1, "patch": [{"op": "replace", "path": "/status", "value": "ready"}],
    "rationale": "r", "expected_effect": "e", "rollback_plan": "b", "idempotency_key": "k1",
}
ITEM_INPUT = {
    "goal_id": GOAL, "type": "epic", "milestone_id": MILESTONE, "title": "t",
    "acceptance_criteria": ["done"], "idempotency_key": "k2",
}


def test_change_set_registered_and_routed_via_services(env):
    mp, calls = env
    res = _run("_work_item_change_set_create", FakeConn(), CS_INPUT, mp)
    assert res["ok"] is True and res["change_set_id"] == "cs-1"
    assert res["required_routes"] == ["project_lead"]
    assert calls["cs"][0][0] == TARGET and calls["route"] == ["cs-1"]
    assert calls["cs"][0][1]["action"] == "update"  # 기본값은 API 모델이 채운다


def test_change_set_denied_without_assignment_or_lead(env):
    mp, calls = env
    res = _run("_work_item_change_set_create", FakeConn(assigned=False, lead=False), CS_INPUT, mp)
    assert res["error"] == "work_item_write_not_permitted"
    assert calls["cs"] == []


def test_change_set_allowed_for_goal_lead_without_assignment(env):
    mp, calls = env
    res = _run("_work_item_change_set_create", FakeConn(assigned=False, lead=True), CS_INPUT, mp)
    assert res["ok"] is True and len(calls["cs"]) == 1


def test_change_set_project_scope_denied(env):
    mp, calls = env
    res = _run("_work_item_change_set_create", FakeConn(project="GO100", sess_project="AADS"), CS_INPUT, mp)
    assert res["error"] == "project_scope_denied" and calls["cs"] == []


def test_change_set_invalid_request_and_missing_session(env):
    mp, calls = env
    bad = {k: v for k, v in CS_INPUT.items() if k != "rollback_plan"}
    assert _run("_work_item_change_set_create", FakeConn(), bad, mp)["error"] == "invalid_request"
    assert _run("_work_item_change_set_create", FakeConn(), CS_INPUT, mp, session="")["error"] == "execute_session_required"
    assert calls["cs"] == []


def test_change_set_service_http_error_code_is_passed_through(env):
    from fastapi import HTTPException

    from app.services import goal_workflow_approval as gwa

    mp, _ = env

    async def boom(conn, **kw):
        raise HTTPException(status_code=409, detail={"code": "version_conflict", "message": "x"})

    mp.setattr(gwa, "create_change_set", boom)
    assert _run("_work_item_change_set_create", FakeConn(), CS_INPUT, mp)["error"] == "version_conflict"


def test_work_item_created_via_service(env):
    mp, calls = env
    res = _run("_work_item_create", FakeConn(), ITEM_INPUT, mp)
    assert res == {"ok": True, "work_item_id": "wi-1", "type": "epic", "status": "draft", "idempotent_replay": False}
    assert calls["item"][0][0] == GOAL
    assert calls["item"][0][1]["idempotency_key"] == "k2"


def test_work_item_requires_acceptance_criteria(env):
    mp, calls = env
    res = _run("_work_item_create", FakeConn(), {**ITEM_INPUT, "acceptance_criteria": []}, mp)
    assert res["error"] == "acceptance_criteria_required" and calls["item"] == []


def test_work_item_denied_without_assignment_or_lead(env):
    mp, calls = env
    res = _run("_work_item_create", FakeConn(assigned=False, lead=False), ITEM_INPUT, mp)
    assert res["error"] == "work_item_write_not_permitted" and calls["item"] == []


def test_work_item_disabled_flag(env):
    mp, _calls = env
    mp.setenv("GOAL_WORK_HIERARCHY_ENABLED", "false")
    assert _run("_work_item_create", FakeConn(), ITEM_INPUT, mp)["error"] == "goal_work_hierarchy_disabled"


def test_tools_registered_and_dispatched():
    from app.services import tool_executor as te
    from app.services import tool_registry as tr

    for name in ("work_item_change_set_create", "work_item_create"):
        assert name in tr._TOOLS and tr._TOOLS[name]["name"] == name
        assert hasattr(te.ToolExecutor, "_" + name)


class _NoQueryConn(FakeConn):
    """세션 검증 전에 DB 를 건드리면 실패하게 하는 conn (리뷰 지적 1)."""

    async def fetchrow(self, sql, *args):
        raise AssertionError(f"DB queried before session validation: {sql}")

    async def fetchval(self, sql, *args):
        raise AssertionError(f"DB queried before session validation: {sql}")


@pytest.mark.parametrize("bad_session", ["not-a-uuid", "", "   ", "1234"])
@pytest.mark.parametrize("name,inp", [
    ("_work_item_change_set_create", CS_INPUT),
    ("_work_item_create", ITEM_INPUT),
])
def test_malformed_session_rejected_before_any_query(env, name, inp, bad_session):
    mp, calls = env
    res = _run(name, _NoQueryConn(), inp, mp, session=bad_session)
    assert res["error"] == "execute_session_required"
    assert calls["cs"] == [] and calls["item"] == []
