"""Destructive M12 migration checks for a dedicated PostgreSQL test database.

Set M12_TEST_DATABASE_URL to a database whose name ends in ``_test``. The
explicit name guard prevents this suite from ever rebuilding a normal DB.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest


asyncpg = pytest.importorskip("asyncpg")
DATABASE_URL = os.getenv("M12_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="M12_TEST_DATABASE_URL is not configured")
ROOT = Path(__file__).parents[2]
UP = (ROOT / "migrations/20260919_goal_work_hierarchy_m12.sql").read_text()
DOWN = (ROOT / "migrations/rollback/20260919_goal_work_hierarchy_m12.down.sql").read_text()


BASELINE = """
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE tenants (id UUID PRIMARY KEY);
CREATE TABLE chat_sessions (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id),
    UNIQUE (id, tenant_id)
);
CREATE TABLE goals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), title TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT 'AADS', status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE milestones (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goal_id UUID NOT NULL REFERENCES goals(id) ON DELETE CASCADE, title TEXT NOT NULL
);
CREATE TABLE agent_permission_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id)
);
CREATE OR REPLACE FUNCTION public.aads_internal_tenant_id() RETURNS UUID
LANGUAGE SQL STABLE AS $$ SELECT id FROM tenants ORDER BY id LIMIT 1 $$;
"""


def _dedicated_test_url() -> str:
    database = urlparse(DATABASE_URL).path.removeprefix("/")
    if not database.endswith("_test"):
        pytest.fail("M12_TEST_DATABASE_URL database name must end in _test")
    return DATABASE_URL


async def _exercise() -> None:
    conn = await asyncpg.connect(_dedicated_test_url())
    try:
        await conn.execute(BASELINE)
        tenant_a, tenant_b = uuid4(), uuid4()
        session_a, session_b = uuid4(), uuid4()
        await conn.execute("INSERT INTO tenants(id) VALUES($1)", tenant_a)
        await conn.execute("INSERT INTO chat_sessions(id, tenant_id) VALUES($1,$2)", session_a, tenant_a)
        goal = await conn.fetchval(
            "INSERT INTO goals(title, project) VALUES('M12', 'AADS') RETURNING id"
        )
        milestone = await conn.fetchval(
            "INSERT INTO milestones(goal_id, title) VALUES($1, 'schema') RETURNING id", goal
        )

        # The same migration is safe twice and legacy milestones receive scope.
        await conn.execute(UP)
        await conn.execute(UP)
        scope = await conn.fetchrow("SELECT tenant_id, project FROM milestones WHERE id=$1", milestone)
        assert scope["tenant_id"] == tenant_a and scope["project"] == "AADS"
        await conn.execute("INSERT INTO tenants(id) VALUES($1)", tenant_b)
        await conn.execute("INSERT INTO chat_sessions(id, tenant_id) VALUES($1,$2)", session_b, tenant_b)

        assignment = await conn.fetchval(
            """INSERT INTO project_role_assignments
               (tenant_id,project,role_key,session_id,assigned_by)
               VALUES($1,'AADS','lead',$2,$2) RETURNING id""",
            tenant_a, session_a,
        )
        epic = await conn.fetchval(
            """INSERT INTO work_items
               (tenant_id,project,goal_id,milestone_id,type,title,assignment_id,idempotency_key)
               VALUES($1,'AADS',$2,$3,'epic','E',$4,'e') RETURNING id""",
            tenant_a, goal, milestone, assignment,
        )
        story = await conn.fetchval(
            """INSERT INTO work_items
               (tenant_id,project,goal_id,milestone_id,parent_id,type,title,assignment_id,idempotency_key)
               VALUES($1,'AADS',$2,$3,$4,'story','S',$5,'s') RETURNING id""",
            tenant_a, goal, milestone, epic, assignment,
        )
        task = await conn.fetchval(
            """INSERT INTO work_items
               (tenant_id,project,goal_id,milestone_id,parent_id,type,title,assignment_id,idempotency_key)
               VALUES($1,'AADS',$2,$3,$4,'task','T',$5,'t') RETURNING id""",
            tenant_a, goal, milestone, story, assignment,
        )

        with pytest.raises(asyncpg.PostgresError):
            await conn.execute("UPDATE work_items SET parent_id=$1 WHERE id=$2", task, story)
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                """INSERT INTO work_items
                   (tenant_id,project,goal_id,milestone_id,parent_id,type,title,idempotency_key)
                   VALUES($1,'OTHER',$2,$3,$4,'task','cross','cross')""",
                tenant_a, goal, milestone, story,
            )

        await conn.execute(
            """INSERT INTO work_item_dependencies
               (tenant_id,project,goal_id,work_item_id,depends_on_id)
               VALUES($1,'AADS',$2,$3,$4)""",
            tenant_a, goal, task, story,
        )
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                """INSERT INTO work_item_dependencies
                   (tenant_id,project,goal_id,work_item_id,depends_on_id)
                   VALUES($1,'AADS',$2,$3,$4)""",
                tenant_a, goal, story, task,
            )

        # Two transactions racing for the same active role: exactly one wins.
        await conn.execute("UPDATE project_role_assignments SET active=FALSE, ended_at=now() WHERE id=$1", assignment)
        async def insert_assignment() -> bool:
            other = await asyncpg.connect(_dedicated_test_url())
            try:
                await other.execute(
                    """INSERT INTO project_role_assignments
                       (tenant_id,project,role_key,session_id,assigned_by)
                       VALUES($1,'AADS','lead',$2,$2)""",
                    tenant_a, session_a,
                )
                return True
            except asyncpg.UniqueViolationError:
                return False
            finally:
                await other.close()
        assert sorted(await asyncio.gather(insert_assignment(), insert_assignment())) == [False, True]

        await conn.execute(DOWN)
        assert not await conn.fetchval("SELECT to_regclass('public.work_items') IS NOT NULL")
    finally:
        await conn.close()


def test_m12_up_twice_constraints_concurrency_and_down():
    asyncio.run(_exercise())
