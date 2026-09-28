"""Real transition and draft persistence on an explicitly disposable PostgreSQL DB."""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest

from app.services.directive_draft_service import validate_directive
from app.services.goal_manager import GoalStateMachine

ROOT = Path(__file__).parents[2]


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def exercise():
    url = os.environ.get("AADS_DRAFT_TEST_DATABASE_URL") or os.environ.get("M12_TEST_DATABASE_URL")
    database = urlparse(url).path.removeprefix("/") if url else os.environ.get("PGDATABASE", "")
    if not database.endswith("_test"):
        pytest.skip("A disposable *_test PostgreSQL database is required")
    if url:
        conn = await asyncio.wait_for(asyncpg.connect(url), timeout=5)
    else:
        conn = await asyncio.wait_for(asyncpg.connect(
            host=os.environ.get("PGHOST", "localhost"),
            port=int(os.environ.get("PGPORT", "5432")),
            user=os.environ.get("PGUSER"), password=os.environ.get("PGPASSWORD"),
            database=database,
        ), timeout=5)
    schema = f"draft_test_{uuid.uuid4().hex}"
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute("CREATE TABLE tenants (id uuid PRIMARY KEY)")
        await conn.execute("CREATE TABLE chat_workspaces (id uuid PRIMARY KEY)")
        await conn.execute(
            """CREATE TABLE chat_sessions
               (id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
                workspace_id uuid NOT NULL REFERENCES chat_workspaces(id))"""
        )
        await conn.execute(
            """CREATE TABLE chat_artifacts
               (id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id uuid NOT NULL REFERENCES tenants(id),
                session_id uuid NOT NULL REFERENCES chat_sessions(id),
                workspace_id uuid REFERENCES chat_workspaces(id),
                type text NOT NULL, title text NOT NULL, content text NOT NULL,
                metadata jsonb NOT NULL DEFAULT '{}', updated_at timestamptz DEFAULT now())"""
        )
        for migration in (
            "153_goal_management.sql",
            "156_goal_control_loop_schema_alignment.sql",
            "172_directive_draft_copilot.sql",
        ):
            await conn.execute((ROOT / "migrations" / migration).read_text())
        await conn.execute((ROOT / "migrations/20260929_milestone_directive_draft_lookup.sql").read_text())
        await conn.execute("ALTER TABLE goals ADD COLUMN tenant_id uuid REFERENCES tenants(id)")
        await conn.execute(
            "ALTER TABLE goal_task_links ADD COLUMN link_state text NOT NULL DEFAULT 'active'"
        )

        tenant, workspace, session = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        goal, completed, following = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await conn.execute("INSERT INTO tenants(id) VALUES($1)", tenant)
        await conn.execute("INSERT INTO chat_workspaces(id) VALUES($1)", workspace)
        await conn.execute(
            "INSERT INTO chat_sessions(id,tenant_id,workspace_id) VALUES($1,$2,$3)",
            session, tenant, workspace,
        )
        await conn.execute(
            """INSERT INTO goals(id,title,project,status,priority,tenant_id)
               VALUES($1,'목표','AADS','active','P1',$2)""",
            goal, tenant,
        )
        await conn.execute(
            """INSERT INTO milestones(id,goal_id,title,sequence_order,status)
               VALUES($1,$2,'완료',1,'completed')""",
            completed, goal,
        )
        await conn.execute(
            """INSERT INTO milestones
               (id,goal_id,title,description,completion_criteria,sequence_order,auto_advance)
               VALUES($1,$2,'후속 단계','코드를 개선','테스트 통과',2,true)""",
            following, goal,
        )
        await conn.execute(
            """INSERT INTO goal_task_links(goal_id,task_type,task_id)
               VALUES($1,'chat_session',$2)""",
            goal, str(session),
        )

        machine = GoalStateMachine()

        async def pool():
            return Pool(conn)

        machine._pool = pool
        await machine._advance_after_milestone(str(goal), str(completed))
        await machine._advance_after_milestone(str(goal), str(completed))

        assert (
            await conn.fetchval("SELECT status FROM milestones WHERE id=$1", following)
            == "in_progress"
        )
        rows = await conn.fetch("SELECT * FROM directive_drafts")
        assert len(rows) == 1
        assert rows[0]["status"] == "draft"
        assert rows[0]["classification"]["milestone_id"] == str(following)
        assert rows[0]["classification"]["auto_submit"] is False
        assert validate_directive(rows[0]["content"], expected_project="AADS")[0]
        assert await conn.fetchval("SELECT count(*) FROM directive_draft_revisions") == 1
        assert await conn.fetchval("SELECT count(*) FROM directive_draft_events") == 1
        assert await conn.fetchval("SELECT count(*) FROM chat_artifacts") == 1
        # A detached link must never receive a new draft.
        await conn.execute(
            "UPDATE goal_task_links SET link_state='detached' WHERE goal_id=$1", goal
        )
        await conn.execute("DELETE FROM directive_drafts")
        await conn.execute("UPDATE milestones SET status='pending' WHERE id=$1", following)
        await machine._advance_after_milestone(str(goal), str(completed))
        assert (
            await conn.fetchval("SELECT status FROM milestones WHERE id=$1", following)
            == "in_progress"
        )
        assert await conn.fetchval("SELECT count(*) FROM directive_drafts") == 0
    finally:
        try:
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await conn.close()


def test_real_auto_advance_creates_one_draft():
    asyncio.run(exercise())
