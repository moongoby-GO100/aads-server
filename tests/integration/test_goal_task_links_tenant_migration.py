"""M6 tenant-link migration checks against a disposable PostgreSQL database."""
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
UP = (ROOT / "migrations/20260919_goal_task_links_tenant_integrity.sql").read_text()

BASELINE = """
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE tenants (id UUID PRIMARY KEY);
CREATE TABLE goals (
  id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id),
  UNIQUE(id, tenant_id)
);
CREATE TABLE milestones (
  id UUID PRIMARY KEY, goal_id UUID NOT NULL REFERENCES goals(id),
  tenant_id UUID NOT NULL REFERENCES tenants(id), UNIQUE(id, tenant_id)
);
CREATE TABLE goal_task_links (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), goal_id UUID REFERENCES goals(id),
  milestone_id UUID REFERENCES milestones(id), task_type TEXT NOT NULL, task_id TEXT NOT NULL
);
"""


def _test_url() -> str:
    if not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail("M12_TEST_DATABASE_URL database name must end in _test")
    return DATABASE_URL


async def _exercise() -> None:
    conn = await asyncpg.connect(_test_url())
    try:
        await conn.execute(BASELINE)
        tenant_a, tenant_b, goal_a, goal_b, milestone_a = (uuid4() for _ in range(5))
        await conn.execute("INSERT INTO tenants VALUES($1),($2)", tenant_a, tenant_b)
        await conn.execute(
            "INSERT INTO goals VALUES($1,$2),($3,$4)",
            goal_a, tenant_a, goal_b, tenant_b,
        )
        await conn.execute(
            "INSERT INTO milestones VALUES($1,$2,$3)",
            milestone_a, goal_a, tenant_a,
        )
        link_id = await conn.fetchval(
            "INSERT INTO goal_task_links(goal_id,milestone_id,task_type,task_id) "
            "VALUES($1,$2,'pipeline_job','legacy') RETURNING id",
            goal_a, milestone_a,
        )

        await conn.execute(UP)
        await conn.execute(UP)
        assert await conn.fetchval(
            "SELECT tenant_id=$2 FROM goal_task_links WHERE id=$1", link_id, tenant_a,
        )

        derived = await conn.fetchval(
            "INSERT INTO goal_task_links(goal_id,task_type,task_id) "
            "VALUES($1,'pipeline_job','derived') RETURNING tenant_id",
            goal_a,
        )
        assert derived == tenant_a
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO goal_task_links(goal_id,tenant_id,task_type,task_id) "
                "VALUES($1,$2,'pipeline_job','cross')",
                goal_a, tenant_b,
            )
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO goal_task_links(goal_id,milestone_id,task_type,task_id) "
                "VALUES($1,$2,'pipeline_job','mixed')",
                goal_b, milestone_a,
            )
    finally:
        await conn.close()


def test_goal_task_link_tenant_backfill_trigger_and_constraints():
    asyncio.run(_exercise())
