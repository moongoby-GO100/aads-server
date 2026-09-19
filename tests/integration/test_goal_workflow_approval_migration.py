"""M14 migration integration checks (requires the dedicated M12 test DB)."""
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
M12 = (ROOT / "migrations/20260919_goal_work_hierarchy_m12.sql").read_text()
M14 = (ROOT / "migrations/20260919_goal_work_hierarchy_m14.sql").read_text()
DOWN14 = (ROOT / "migrations/rollback/20260919_goal_work_hierarchy_m14.down.sql").read_text()


BASELINE = """
DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE tenants(id UUID PRIMARY KEY);
CREATE TABLE chat_sessions(id UUID PRIMARY KEY,tenant_id UUID NOT NULL REFERENCES tenants(id),UNIQUE(id,tenant_id));
CREATE TABLE goals(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),title TEXT NOT NULL,project TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE milestones(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),goal_id UUID NOT NULL REFERENCES goals(id),title TEXT NOT NULL);
CREATE TABLE agent_permission_requests(id UUID PRIMARY KEY DEFAULT gen_random_uuid(),tenant_id UUID NOT NULL REFERENCES tenants(id));
CREATE OR REPLACE FUNCTION public.aads_internal_tenant_id() RETURNS UUID LANGUAGE SQL STABLE
AS $$ SELECT id FROM tenants ORDER BY id LIMIT 1 $$;
"""


def _url() -> str:
    if not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail("M12_TEST_DATABASE_URL database name must end in _test")
    return DATABASE_URL


async def _exercise() -> None:
    conn = await asyncpg.connect(_url())
    try:
        await conn.execute(BASELINE)
        tenant, session = uuid4(), uuid4()
        await conn.execute("INSERT INTO tenants VALUES($1)", tenant)
        await conn.execute("INSERT INTO chat_sessions VALUES($1,$2)", session, tenant)
        await conn.execute("INSERT INTO goals(title,project) VALUES('M14','AADS')")
        await conn.execute(M12)
        await conn.execute(M14)
        await conn.execute(M14)
        decision = uuid4()
        await conn.execute(
            "INSERT INTO goal_approval_decision_logs(id,tenant_id,decision) VALUES($1,$2,'DENY')",
            decision, tenant,
        )
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute("UPDATE goal_approval_decision_logs SET decision='AUTO' WHERE id=$1", decision)
        await conn.execute(DOWN14)
        assert not await conn.fetchval("SELECT to_regclass('goal_approval_decision_logs') IS NOT NULL")
    finally:
        await conn.close()


def test_m14_up_twice_append_only_and_down():
    asyncio.run(_exercise())
