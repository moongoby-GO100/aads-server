"""Disposable PostgreSQL proof for goal owner resolution, backfill and the NOT VALID CHECK.

Set M12_TEST_DATABASE_URL to a database whose name ends in ``_test``. Everything runs in a
throw-away schema that is dropped at the end; no table in ``public`` is touched.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest

asyncpg = pytest.importorskip("asyncpg")

from app.services.goal_owner_resolver import (  # noqa: E402
    find_goals_missing_owner,
    plan_owner_fix,
    resolve_goal_owner,
)

DATABASE_URL = os.getenv("M12_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="M12_TEST_DATABASE_URL is not configured")
ROOT = Path(__file__).parents[2]
CHECK_UP = (ROOT / "migrations/20261023_goals_owner_required_check.sql").read_text()
CHECK_DOWN = (ROOT / "migrations/rollback/20261023_goals_owner_required_check.down.sql").read_text()
SCHEMA = "goal_owner_it"

DDL = f"""
CREATE SCHEMA {SCHEMA};
SET search_path = {SCHEMA};
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE chat_workspaces(id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL,
    project_key TEXT, name TEXT, display_name TEXT);
CREATE TABLE chat_sessions(id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL,
    workspace_id UUID NOT NULL REFERENCES chat_workspaces(id), title TEXT, role_key TEXT,
    user_id TEXT, updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE project_role_assignments(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL, project TEXT NOT NULL, role_key TEXT NOT NULL,
    session_id UUID NOT NULL REFERENCES chat_sessions(id), active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE UNIQUE INDEX ON project_role_assignments(tenant_id, project, role_key) WHERE active;
CREATE TABLE goals(id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL,
    project TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    owner_role_key TEXT, owner_session_id UUID, created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(), description TEXT, success_criteria TEXT, priority TEXT,
    parent_goal_id UUID);
CREATE TABLE milestones(id UUID PRIMARY KEY, goal_id UUID NOT NULL REFERENCES goals(id),
    tenant_id UUID NOT NULL, project TEXT NOT NULL, title TEXT, description TEXT,
    completion_criteria TEXT, sequence_order INT, auto_advance BOOLEAN, status TEXT,
    owner_role_key TEXT, owner_session_id UUID, started_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE work_items(id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL,
    project TEXT NOT NULL, goal_id UUID NOT NULL REFERENCES goals(id),
    milestone_id UUID NOT NULL REFERENCES milestones(id), parent_id UUID, type TEXT NOT NULL,
    title TEXT, description TEXT, acceptance_criteria JSONB, priority TEXT, assignment_id UUID,
    idempotency_key TEXT, created_by UUID);
CREATE TABLE goal_task_links(goal_id UUID, milestone_id UUID, tenant_id UUID, task_type TEXT,
    task_id TEXT, status TEXT);
CREATE UNIQUE INDEX ON goal_task_links(goal_id, task_type, task_id) WHERE goal_id IS NOT NULL;
CREATE TABLE pipeline_jobs(job_id TEXT, tenant_id UUID, project TEXT);
"""


def _url() -> str:
    if not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail("M12_TEST_DATABASE_URL database name must end in _test")
    return DATABASE_URL


async def _session(conn, tenant, project, role, user_id="u1") -> str:
    ws = await conn.fetchval(
        "INSERT INTO chat_workspaces(tenant_id,project_key,name) VALUES($1,$2,$2) RETURNING id",
        tenant, project,
    )
    return str(await conn.fetchval(
        "INSERT INTO chat_sessions(tenant_id,workspace_id,title,role_key,user_id) "
        "VALUES($1,$2,'t',$3,$4) RETURNING id",
        tenant, ws, role, user_id,
    ))


async def _exercise() -> None:
    conn = await asyncpg.connect(_url())
    ta, tb = uuid4(), uuid4()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.execute(DDL)
        dev = await _session(conn, ta, "AADS", "dev")
        qa = await _session(conn, ta, "AADS", "qa")
        other_proj = await _session(conn, ta, "GO100", "dev")
        other_tenant = await _session(conn, tb, "AADS", "dev")
        ceo = await _session(conn, ta, "CEO", "ceo")
        await conn.execute(
            "INSERT INTO project_role_assignments(tenant_id,project,role_key,session_id) "
            "VALUES($1,'AADS','dev',$2)", ta, UUID(dev),
        )

        ok = await resolve_goal_owner(conn, tenant_id=str(ta), project="AADS", owner_role_key="dev")
        assert (ok.ok, ok.owner_session_id) == (True, dev)
        creator = await resolve_goal_owner(conn, tenant_id=str(ta), project="AADS", creator_session_id=qa)
        assert (creator.owner_role_key, creator.owner_session_id) == ("qa", qa)
        for bad_sid, code in ((other_proj, "cross_project_owner_denied"),
                              (other_tenant, "owner_session_not_found"),
                              (ceo, "cross_project_owner_denied")):
            res = await resolve_goal_owner(conn, tenant_id=str(ta), project="AADS", owner_session_id=bad_sid)
            assert res.error == code and res.owner_session_id is None
        required = await resolve_goal_owner(conn, tenant_id=str(ta), project="AADS")
        assert required.error == "owner_required"
        assert {c["session_id"] for c in required.candidates} == {dev, qa}

        # 보정: 후보가 둘(dev 배정 + qa 역할 세션)이므로 모호 → 쓰지 않는다.
        gid = await conn.fetchval(
            "INSERT INTO goals(tenant_id,project,title) VALUES($1,'AADS','g') RETURNING id", ta)
        missing = await find_goals_missing_owner(conn)
        assert [m["goal_id"] for m in missing] == [str(gid)]
        assert (await plan_owner_fix(conn, missing[0]))["fix"] is None
        # 부분 입력(역할만 있음)은 배정으로 확정된다.
        await conn.execute("UPDATE goals SET owner_role_key='dev' WHERE id=$1", gid)
        plan = await plan_owner_fix(conn, (await find_goals_missing_owner(conn))[0])
        assert plan["fix"] == {"owner_role_key": "dev", "owner_session_id": dev}

        # NOT VALID CHECK: 기존 누락 행은 남고, 신규 누락 INSERT 는 막히며, down 으로 사라진다.
        await conn.execute(CHECK_UP)
        await conn.execute(CHECK_UP)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("INSERT INTO goals(tenant_id,project,title) VALUES($1,'AADS','x')", ta)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE goals SET title='y' WHERE id=$1", gid)
        await conn.execute("INSERT INTO goals(tenant_id,project,title,status) VALUES($1,'AADS','x','completed')", ta)
        await conn.execute(
            "UPDATE goals SET owner_session_id=$2::uuid, title='y' WHERE id=$1", gid, dev)
        await conn.execute(
            "INSERT INTO goals(tenant_id,project,title,owner_role_key,owner_session_id) "
            "VALUES($1,'AADS','z','dev',$2::uuid)", ta, dev)
        assert await find_goals_missing_owner(conn) == []
        await conn.execute(CHECK_DOWN)
        await conn.execute("INSERT INTO goals(tenant_id,project,title) VALUES($1,'AADS','after-down')", ta)

        # 원자성: 담당 확정 후 같은 트랜잭션에서 실패하면 목표 행이 남지 않는다.
        before = await conn.fetchval("SELECT count(*) FROM goals")
        with pytest.raises(RuntimeError):
            async with conn.transaction():
                res = await resolve_goal_owner(conn, tenant_id=str(ta), project="AADS", owner_role_key="dev")
                await conn.execute(
                    "INSERT INTO goals(tenant_id,project,title,owner_role_key,owner_session_id) "
                    "VALUES($1,'AADS','rolled',$2,$3::uuid)", ta, res.owner_role_key, res.owner_session_id)
                raise RuntimeError("boom")
        assert await conn.fetchval("SELECT count(*) FROM goals") == before
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.close()


def test_goal_owner_resolution_backfill_and_check_on_postgres() -> None:
    asyncio.run(_exercise())


def _goal_input(project: str = "AADS") -> dict:
    epic = {"title": "에픽", "acceptance_criteria": ["기준"],
            "stories": [{"title": "스토리", "acceptance_criteria": ["기준"],
                         "tasks": [{"title": "작업", "acceptance_criteria": ["기준"]}]}]}
    return {
        "goal": {"project": project, "title": "담당 자동확정 목표", "priority": "P1",
                 "description": "설명", "success_criteria": "기준"},
        "milestones": [
            {"title": f"마일스톤{i}", "description": "설명", "completion_criteria": "기준",
             "sequence_order": i, "auto_advance": True, "epics": [epic]}
            for i in (1, 2, 3)
        ],
    }


async def _counts(conn) -> tuple[int, int, int]:
    goals = await conn.fetchval("SELECT count(*) FROM goals")
    milestones = await conn.fetchval("SELECT count(*) FROM milestones")
    work_items = await conn.fetchval("SELECT count(*) FROM work_items")
    return goals, milestones, work_items


async def _exercise_register(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.services import goal_work_hierarchy as hierarchy
    from app.services.goal_manager import goal_state_machine
    from app.services.goal_registration import register_goal

    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(goal_state_machine, "_trace", _noop)
    monkeypatch.setattr(goal_state_machine, "_trace_policy", _noop)

    conn = await asyncpg.connect(_url())
    ta, tb = uuid4(), uuid4()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.execute(DDL)
        dev = await _session(conn, ta, "AADS", "dev")
        foreign_tenant = await _session(conn, tb, "AADS", "dev")
        other_proj = await _session(conn, ta, "GO100", "dev")
        assert await _counts(conn) == (0, 0, 0)

        # 1) 성공: 생성 세션(도구 경로)이 같은 project 담당이면 확정되어 목표·마일스톤에 기록된다.
        res = await register_goal(conn, _goal_input(), tenant_id=str(ta), session_id=dev, activate=True)
        assert "error" not in res, res
        gid = res["goal_id"]
        row = await conn.fetchrow("SELECT owner_role_key, owner_session_id::text AS sid FROM goals WHERE id=$1::uuid", gid)
        assert (row["owner_role_key"], row["sid"]) == ("dev", dev)
        assert await conn.fetchval(
            "SELECT count(*) FROM milestones WHERE goal_id=$1::uuid AND owner_role_key='dev' "
            "AND owner_session_id=$2::uuid", gid, UUID(dev)) == 3
        assert await conn.fetchval("SELECT count(*) FROM work_items WHERE goal_id=$1::uuid", gid) == 9
        assert await conn.fetchval(
            "SELECT count(*) FROM goal_task_links WHERE goal_id=$1::uuid AND task_id=$2", gid, dev) == 1
        baseline = await _counts(conn)
        assert baseline == (1, 3, 9)

        # 2) owner_required: REST 경로(actor 만 있고 힌트 없음)는 422 대신 error 를 돌려주고 0행을 쓴다.
        actor = await hierarchy.resolve_actor_scope(
            conn, tenant_id=str(ta), user_id="u1", actor_session_id=dev)
        res = await register_goal(conn, _goal_input(), tenant_id=str(ta), actor=actor, activate=True)
        assert res["error"] == "owner_required"
        assert {c["session_id"] for c in res["candidates"]} == {dev}
        assert await _counts(conn) == baseline

        # 3) 타 tenant 세션 거부: 담당으로 지정해도, 생성 세션으로 써도 행이 생기지 않는다.
        with pytest.raises(HTTPException) as exc:
            await register_goal(conn, _goal_input(), tenant_id=str(ta), actor=actor,
                                owner_session_id=foreign_tenant)
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            await register_goal(conn, _goal_input(), tenant_id=str(ta), session_id=foreign_tenant)
        assert exc.value.status_code == 403
        assert await _counts(conn) == baseline

        # 4) 타 project 세션을 담당으로 지정하면 접근 거부(403) 되고 행이 없다.
        with pytest.raises(HTTPException) as exc:
            await register_goal(conn, _goal_input(), tenant_id=str(ta), actor=actor,
                                owner_session_id=other_proj)
        assert exc.value.status_code == 403
        assert await _counts(conn) == baseline
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.close()


def test_register_goal_confirms_owner_or_writes_nothing_on_postgres(monkeypatch) -> None:
    asyncio.run(_exercise_register(monkeypatch))
