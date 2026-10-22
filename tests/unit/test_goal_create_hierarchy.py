"""All public goal creation paths must reject incomplete hierarchies before writes."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers import goals
from app.services.goal_manager import GoalStateMachine
from app.services.goal_planner import GoalPlanner, MilestoneSpec
from tests.unit.test_goal_registration import SESSION, MemoryConnection, TENANT, payload


@pytest.fixture
def api(monkeypatch):
    from app.core import db_pool

    conn = MemoryConnection()
    conn.execute = AsyncMock(wraps=conn.execute)
    conn.fetchrow = AsyncMock(wraps=conn.fetchrow)

    @asynccontextmanager
    async def acquire():
        yield conn

    class Pool:
        pass

    pool = Pool()
    pool.acquire = acquire
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    app = FastAPI()
    app.include_router(goals.router, prefix="/api/v1")
    async def member_context():
        return {"tenant": {"id": TENANT}, "user": {"user_id": "user"}}

    app.dependency_overrides[goals.require_tenant_member] = member_context
    class Client:
        def post(self, path, *, json):
            async def request():
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    return await client.post(path, json=json)
            return asyncio.run(request())

    yield Client(), conn


def rest_payload():
    p = payload()
    return {**p["goal"], "milestones": [
        {**ms, "sequence": ms["sequence_order"]} for ms in p["milestones"]
    ], "owner_session_id": SESSION}


def rest_payload_without_owner():
    body = rest_payload()
    body.pop("owner_session_id")
    return body


@pytest.mark.parametrize("missing", ["milestones", "epics"])
def test_rest_rejects_before_insert(api, missing):
    client, conn = api
    p = rest_payload()
    if missing == "milestones":
        p["milestones"] = []
    else:
        for ms in p["milestones"]:
            ms.pop("epics")
    response = client.post("/api/v1/goals", json=p)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_goal_hierarchy"
    assert not conn.goals and not conn.milestones and not conn.items and not conn.links
    conn.execute.assert_not_awaited()


def test_rest_complete_hierarchy_saved_without_session(api):
    client, conn = api
    response = client.post("/api/v1/goals", json=rest_payload())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "draft"
    assert len(conn.goals) == 1 and len(conn.milestones) == 3 and len(conn.items) == 9
    assert all(g["owner_session_id"] == SESSION and g["owner_role_key"] == "dev"
               for g in conn.goals.values())
    assert all(m["owner_session_id"] == SESSION for m in conn.milestones.values())
    assert all(g["status"] == "draft" for g in conn.goals.values())


def test_rest_without_owner_is_rejected_even_for_draft(api):
    client, conn = api
    response = client.post("/api/v1/goals", json=rest_payload_without_owner())
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "owner_required" and "candidates" in detail
    assert not conn.goals and not conn.milestones and not conn.items and not conn.links
    conn.execute.assert_not_awaited()


def test_rest_failure_rolls_back(api):
    client, conn = api
    conn.fail_at = 5
    with pytest.raises(RuntimeError, match="injected"):
        client.post("/api/v1/goals", json=rest_payload())
    assert not conn.goals and not conn.milestones and not conn.items and not conn.links


def test_rest_p0_cap_preserved(api, monkeypatch):
    from app.services import goal_signals
    client, conn = api
    monkeypatch.setattr(goal_signals, "active_p0_goals", AsyncMock(return_value=[{}] * 3))
    p = rest_payload()
    p.update(priority="P0", activate=True)
    response = client.post("/api/v1/goals", json=p)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "p0_cap_exceeded"
    assert not conn.goals


@pytest.mark.asyncio
async def test_planner_rejects_standalone_and_inexpressible_plan():
    planner = GoalPlanner()
    with pytest.raises(ValueError, match="invalid_goal_hierarchy"):
        await planner.create_goal("목표")
    with pytest.raises(ValueError, match="invalid_goal_hierarchy"):
        await planner.create_plan("목표", [MilestoneSpec("단계")] * 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("validated", [False, True])
async def test_low_level_requires_explicit_validation(monkeypatch, validated):
    machine = GoalStateMachine()
    pool = AsyncMock()
    monkeypatch.setattr(machine, "_pool", pool)
    assert await machine.create_goal("AADS", "목표", hierarchy_validated=validated) == {"error": "invalid_goal_hierarchy"}
    pool.assert_not_called()


def test_rest_activate_starts_first_milestone_atomically(api):
    client, conn = api
    p = rest_payload()
    p["activate"] = True
    p["milestones"][0]["sequence"] = 10
    response = client.post("/api/v1/goals", json=p)
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    milestones = list(conn.milestones.values())
    assert [m["status"] for m in milestones] == ["pending", "in_progress", "pending"]
    assert milestones[1]["started_at"] is not None


def test_rest_rejects_other_users_owner_session(api):
    from uuid import uuid4
    client, conn = api
    original = conn.fetchrow

    async def fetchrow(sql, *args):
        row = await original(sql, *args)
        if "workspace_name" in sql:
            row["user_id"] = "another-user"
        return row

    conn.fetchrow = fetchrow
    p = rest_payload()
    p["owner_session_id"] = str(uuid4())
    response = client.post("/api/v1/goals", json=p)
    assert response.status_code == 403
    conn.execute.assert_not_awaited()
    assert not conn.goals and not conn.items


def test_rest_owned_session_keeps_authenticated_creator(api):
    client, conn = api
    p = rest_payload()
    p["owner_session_id"] = SESSION
    response = client.post("/api/v1/goals", json=p)
    assert response.status_code == 200
    goal_calls = [call for call in conn.execute.await_args_list if "INSERT INTO goals" in call.args[0]]
    assert goal_calls[0].args[10] == SESSION
    work_calls = [call for call in conn.fetchrow.await_args_list if "INSERT INTO work_items" in call.args[0]]
    assert len(work_calls) == 9
    # REST has no execution session: the requested owner is not the creator.
    assert all(call.args[13] is None for call in work_calls)
