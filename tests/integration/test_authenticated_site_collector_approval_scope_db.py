"""Opt-in PostgreSQL transaction checks for M3 account-approval isolation."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest

asyncpg = pytest.importorskip("asyncpg")
TEST_DATABASE_URL = os.getenv("AADS_AUTHENTICATED_SITE_COLLECTOR_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason=(
        "set AADS_AUTHENTICATED_SITE_COLLECTOR_TEST_DATABASE_URL "
        "to an isolated *_test database"
    ),
)
ROOT = Path(__file__).parents[2]
MIGRATION = (ROOT / "migrations/20260919_authenticated_site_account_approval_scope.sql").read_text()


BASELINE = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS tenants(
    id UUID PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'customer', status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS agent_permission_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
    work_key TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '', action_type TEXT NOT NULL,
    action_summary TEXT NOT NULL DEFAULT '', risk_level TEXT NOT NULL DEFAULT 'medium',
    decision TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '10 minutes'),
    approval_scope JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS agent_vault_credentials (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id), work_key TEXT NOT NULL,
    origin TEXT NOT NULL, label TEXT NOT NULL DEFAULT 'default', username_enc TEXT NOT NULL,
    password_enc TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE, created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS authenticated_site_profiles (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id), project_key TEXT NOT NULL,
    site_key TEXT NOT NULL, display_name TEXT NOT NULL, base_origin TEXT NOT NULL,
    allowed_origins JSONB NOT NULL DEFAULT '[]'::jsonb, runtime TEXT NOT NULL,
    data_categories JSONB NOT NULL DEFAULT '[]'::jsonb,
    login_mode TEXT NOT NULL DEFAULT 'user_session',
    challenge_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    retention_policy JSONB NOT NULL DEFAULT '{}'::jsonb, enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb, created_by TEXT NOT NULL DEFAULT '',
    UNIQUE (tenant_id, project_key, site_key)
);
CREATE TABLE IF NOT EXISTS authenticated_site_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
    site_profile_id UUID NOT NULL REFERENCES authenticated_site_profiles(id),
    account_label TEXT NOT NULL,
    vault_reference TEXT NOT NULL DEFAULT '', login_status TEXT NOT NULL DEFAULT 'login_required',
    last_authenticated_at TIMESTAMPTZ NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, site_profile_id, account_label)
);
"""


def _url() -> str:
    if not urlparse(TEST_DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail(
            "AADS_AUTHENTICATED_SITE_COLLECTOR_TEST_DATABASE_URL "
            "database name must end in _test"
        )
    return TEST_DATABASE_URL


def _database_dependencies():
    """Delay application imports until the opt-in asyncpg dependency exists."""
    from app.core import db_pool
    from app.services import authenticated_site_collector

    return db_pool, authenticated_site_collector


async def _exercise(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = await asyncpg.connect(_url())
    pool = None
    tenant_ids: list[object] = []
    try:
        await conn.execute(BASELINE)
        await conn.execute(MIGRATION)
        await conn.execute(MIGRATION)  # additive/idempotent migration contract
        run_id = uuid4().hex
        tenant_a, tenant_b = uuid4(), uuid4()
        tenant_ids = [tenant_a, tenant_b]
        profile_a, profile_b = uuid4(), uuid4()
        vault_a, vault_b = uuid4(), uuid4()
        origin = f"https://{run_id}.example.test"
        work_key_a = f"collector-{run_id}-a"
        work_key_b = f"collector-{run_id}-b"
        account_label = f"primary-{run_id}"
        await conn.executemany(
            "INSERT INTO tenants(id,slug,name,kind,status,metadata) "
            "VALUES($1,$2,$3,'customer','active','{}'::jsonb)",
            [
                (tenant_a, f"collector-{run_id}-a", f"Collector fixture {run_id} A"),
                (tenant_b, f"collector-{run_id}-b", f"Collector fixture {run_id} B"),
            ],
        )
        await conn.executemany(
            "INSERT INTO authenticated_site_profiles"
            "(id,tenant_id,project_key,site_key,display_name,base_origin,allowed_origins,runtime,"
            "data_categories,login_mode,challenge_policy,retention_policy,enabled,metadata,"
            "created_by) "
            "VALUES($1,$2,'CUSTOM',$3,$4,$5,$6::jsonb,'playwright_server',$7::jsonb,"
            "'user_session','{}'::jsonb,'{}'::jsonb,TRUE,'{}'::jsonb,'integration-test')",
            [
                (
                    profile_a, tenant_a, f"portal-{run_id}-a", f"Portal {run_id} A", origin,
                    f'["{origin}"]', '["analytics"]',
                ),
                (
                    profile_b, tenant_b, f"portal-{run_id}-b", f"Portal {run_id} B", origin,
                    f'["{origin}"]', '["analytics"]',
                ),
            ],
        )
        await conn.executemany(
            "INSERT INTO agent_vault_credentials"
            "(id,tenant_id,work_key,origin,label,username_enc,password_enc,metadata,is_active,"
            "created_by) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,'{}'::jsonb,TRUE,'integration-test')",
            [
                (
                    vault_a, tenant_a, work_key_a, origin, f"fixture-{run_id}-a", "fixture-user-a",
                    "fixture-password-a",
                ),
                (
                    vault_b, tenant_b, work_key_b, origin, f"fixture-{run_id}-b", "fixture-user-b",
                    "fixture-password-b",
                ),
            ],
        )

        # The foreign-key rejection is verified inside an explicit transaction
        # and rolled back so this evidence leaves no account/approval rows.
        foreign_approval = await conn.fetchval(
            "INSERT INTO agent_permission_requests"
            "(id,tenant_id,work_key,origin,action_type,action_summary,risk_level,decision,reason,"
            "requested_by,expires_at,approval_scope) "
            "VALUES($1,$2,$3,$4,'account_credential_use',$5,'medium','pending',$6,"
            "'integration-test',NOW() + INTERVAL '10 minutes','{}'::jsonb) RETURNING id",
            uuid4(), tenant_b, work_key_b, origin, f"Foreign approval {run_id}",
            f"Fixture {run_id}",
        )
        await conn.execute("BEGIN")
        try:
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "INSERT INTO authenticated_site_accounts(tenant_id,site_profile_id,"
                    "account_label,"
                    "vault_reference,credential_approval_request_id) VALUES($1,$2,'cross',$3,$4)",
                    tenant_a, profile_a, str(vault_a), foreign_approval,
                )
        finally:
            await conn.execute("ROLLBACK")

        pool = await asyncpg.create_pool(_url(), min_size=1, max_size=2)
        pool_module, collector = _database_dependencies()
        monkeypatch.setattr(collector, "_db_enabled", lambda: True)
        monkeypatch.setattr(pool_module, "get_pool", lambda: pool)

        first, second = await asyncio.gather(
            collector.request_first_login(
                tenant_id=str(tenant_a), user_id="member-a", site_profile_id=str(profile_a),
                account_label=account_label, vault_reference=str(vault_a),
            ),
            collector.request_first_login(
                tenant_id=str(tenant_a), user_id="member-a", site_profile_id=str(profile_a),
                account_label=account_label, vault_reference=str(vault_a),
            ),
        )
        assert len({first["account"]["approval"]["id"], second["account"]["approval"]["id"]}) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM agent_permission_requests "
            "WHERE tenant_id=$1 AND action_type='account_credential_use'",
            tenant_a,
        ) == 1
        assert await conn.fetchval(
            "SELECT work_key FROM agent_permission_requests "
            "WHERE tenant_id=$1 AND action_type='account_credential_use'",
            tenant_a,
        ) == work_key_a
        assert await conn.fetchval(
            "SELECT count(*) FROM authenticated_site_accounts a "
            "JOIN agent_permission_requests r ON r.id=a.credential_approval_request_id "
            "AND r.tenant_id=a.tenant_id WHERE a.tenant_id=$1",
            tenant_a,
        ) == 1
    finally:
        if pool is not None:
            await pool.close()
        try:
            if tenant_ids:
                await conn.execute(
                    "DELETE FROM authenticated_site_accounts WHERE tenant_id = ANY($1::uuid[])",
                    tenant_ids,
                )
                await conn.execute(
                    "DELETE FROM agent_permission_requests WHERE tenant_id = ANY($1::uuid[])",
                    tenant_ids,
                )
                await conn.execute(
                    "DELETE FROM agent_vault_credentials WHERE tenant_id = ANY($1::uuid[])",
                    tenant_ids,
                )
                await conn.execute(
                    "DELETE FROM authenticated_site_profiles WHERE tenant_id = ANY($1::uuid[])",
                    tenant_ids,
                )
                await conn.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", tenant_ids)
                assert await conn.fetchval(
                    "SELECT count(*) FROM tenants WHERE id = ANY($1::uuid[])", tenant_ids
                ) == 0
        finally:
            await conn.close()


def test_account_approval_scope_real_postgres_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_exercise(monkeypatch))
