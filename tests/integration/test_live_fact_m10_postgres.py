"""M10 migration contract against an explicitly disposable PostgreSQL database."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

asyncpg = pytest.importorskip("asyncpg")
ROOT = Path(__file__).resolve().parents[2]
UP = (ROOT / "migrations/20260920_m10_live_fact_freshness.sql").read_text()
DOWN = (ROOT / "migrations/rollback/20260920_m10_live_fact_freshness.down.sql").read_text()


def _url() -> str:
    url = os.environ.get("SMART_BROWSER_M10_TEST_DATABASE_URL", "")
    database = urlsplit(url).path.removeprefix("/")
    if not url or not database.startswith("smartbrowser_m10_"):
        pytest.fail("SMART_BROWSER_M10_TEST_DATABASE_URL must target a disposable smartbrowser_m10_* database")
    return url


BOOTSTRAP = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE tenants(id uuid PRIMARY KEY);
CREATE TABLE authenticated_site_profiles(
    id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id)
);
CREATE TABLE browser_live_facts(
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants(id),
    fact_type text NOT NULL, entity_key text NOT NULL, variant_key text NOT NULL DEFAULT '',
    account_context_hash text NOT NULL DEFAULT '', source_url text NOT NULL,
    source_kind text NOT NULL, revalidator_key text NOT NULL, observed_value jsonb NOT NULL,
    observed_value_hash text NOT NULL, observed_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL, revalidated_at timestamptz NOT NULL,
    freshness_status text NOT NULL, evidence_id text, evidence jsonb NOT NULL DEFAULT '{}',
    version bigint NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(), site_profile_id uuid, provenance jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE browser_live_fact_events(
    id bigserial PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
    fact_id uuid NOT NULL REFERENCES browser_live_facts(id), status text NOT NULL,
    value_hash text NOT NULL, evidence_id text, evidence jsonb NOT NULL DEFAULT '{}',
    observed_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
"""


@pytest.mark.asyncio
async def test_m10_twice_rollback_reapply_tenant_constraints_and_rls() -> None:
    conn = await asyncpg.connect(_url())
    tenant_a, tenant_b, profile_a = uuid4(), uuid4(), uuid4()
    try:
        await conn.execute(BOOTSTRAP)
        await conn.execute(UP)
        await conn.execute(UP)
        await conn.execute("INSERT INTO tenants(id) VALUES($1),($2)", tenant_a, tenant_b)
        await conn.execute(
            "INSERT INTO authenticated_site_profiles(id,tenant_id) VALUES($1,$2)", profile_a, tenant_a,
        )
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                """INSERT INTO browser_live_facts
                   (tenant_id,fact_type,entity_key,source_url,source_kind,revalidator_key,
                    observed_value,observed_value_hash,observed_at,fetched_at,expires_at,revalidated_at,
                    freshness_status,evidence_id,site_profile_id)
                   VALUES($1,'price','p','https://x/_source/'||repeat('a',64),'test','r','1','h',
                          now(),now(),now()+interval '1 minute',now(),'CURRENT','e',$2)""",
                tenant_b, profile_a,
            )
        await conn.execute("CREATE ROLE m10_tenant_test NOLOGIN")
        await conn.execute("GRANT SELECT,INSERT ON browser_live_facts TO m10_tenant_test")
        await conn.execute("SET ROLE m10_tenant_test")
        await conn.execute("SELECT set_config('app.current_tenant_id',$1,false)", str(tenant_a))
        assert await conn.fetchval("SELECT count(*) FROM browser_live_facts") == 0
        await conn.execute("RESET ROLE")
        await conn.execute(DOWN)
        await conn.execute(UP)
        assert await conn.fetchval(
            "SELECT fetched_at IS NOT NULL FROM browser_live_facts LIMIT 1"
        ) is None
    finally:
        await conn.close()
