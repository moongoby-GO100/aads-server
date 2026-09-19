"""Opt-in PostgreSQL transaction checks for M3 account-approval isolation."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pytest

asyncpg = pytest.importorskip("asyncpg")
DATABASE_URL = os.getenv("AADS_AUTHENTICATED_SITE_COLLECTOR_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason=(
        "set AADS_AUTHENTICATED_SITE_COLLECTOR_TEST_DATABASE_URL "
        "to an isolated *_test database"
    ),
)
ROOT = Path(__file__).parents[2]
MIGRATION = (ROOT / "migrations/20260919_authenticated_site_account_approval_scope.sql").read_text()


BASELINE = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS tenants(id UUID PRIMARY KEY);
CREATE TABLE IF NOT EXISTS agent_permission_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
    work_key TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '', action_type TEXT NOT NULL,
    action_summary TEXT NOT NULL DEFAULT '', risk_level TEXT NOT NULL DEFAULT 'medium',
    decision TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '', requested_by TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '10 minutes'), approval_scope JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS agent_vault_credentials (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id), work_key TEXT NOT NULL,
    origin TEXT NOT NULL, is_active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS authenticated_site_profiles (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES tenants(id), site_key TEXT NOT NULL,
    base_origin TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authenticated_site_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL REFERENCES tenants(id),
    site_profile_id UUID NOT NULL REFERENCES authenticated_site_profiles(id), account_label TEXT NOT NULL,
    vault_reference TEXT NOT NULL DEFAULT '', login_status TEXT NOT NULL DEFAULT 'login_required',
    last_authenticated_at TIMESTAMPTZ NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (tenant_id, site_profile_id, account_label)
);
"""


def _url() -> str:
    if not urlparse(DATABASE_URL).path.removeprefix("/").endswith("_test"):
        pytest.fail(
            "AADS_AUTHENTICATED_SITE_COLLECTOR_TEST_DATABASE_URL "
            "database name must end in _test"
        )
    return DATABASE_URL


async def _exercise(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = await asyncpg.connect(_url())
    pool = None
    try:
        await conn.execute(BASELINE)
        await conn.execute(MIGRATION)
        await conn.execute(MIGRATION)  # additive/idempotent migration contract
        tenant_a, tenant_b = uuid4(), uuid4()
        profile_a, profile_b = uuid4(), uuid4()
        vault_a, vault_b = uuid4(), uuid4()
        await conn.executemany(
            "INSERT INTO tenants(id) VALUES($1)", [(tenant_a,), (tenant_b,)]
        )
        await conn.executemany(
            "INSERT INTO authenticated_site_profiles"
            "(id,tenant_id,site_key,base_origin) VALUES($1,$2,$3,$4)",
            [
                (profile_a, tenant_a, "portal.a", "https://portal.example"),
                (profile_b, tenant_b, "portal.b", "https://portal.example"),
            ],
        )
        await conn.executemany(
            "INSERT INTO agent_vault_credentials(id,tenant_id,work_key,origin) "
            "VALUES($1,$2,$3,$4)",
            [
                (vault_a, tenant_a, "tenant-a-work", "https://portal.example"),
                (vault_b, tenant_b, "tenant-b-work", "https://portal.example"),
            ],
        )

        # The foreign-key rejection is verified inside an explicit transaction
        # and rolled back so this evidence leaves no account/approval rows.
        foreign_approval = await conn.fetchval(
            "INSERT INTO agent_permission_requests(tenant_id,work_key,origin,action_type) "
            "VALUES($1,'other','https://portal.example','account_credential_use') RETURNING id",
            tenant_b,
        )
        await conn.execute("BEGIN")
        try:
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "INSERT INTO authenticated_site_accounts(tenant_id,site_profile_id,account_label,"
                    "vault_reference,credential_approval_request_id) VALUES($1,$2,'cross',$3,$4)",
                    tenant_a, profile_a, str(vault_a), foreign_approval,
                )
        finally:
            await conn.execute("ROLLBACK")

        pool = await asyncpg.create_pool(_url(), min_size=1, max_size=2)
        monkeypatch.setenv("DATABASE_URL", _url())
        monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
        from app.services.authenticated_site_collector import request_first_login

        first, second = await asyncio.gather(
            request_first_login(
                tenant_id=str(tenant_a), user_id="member-a", site_profile_id=str(profile_a),
                account_label="primary", vault_reference=str(vault_a),
            ),
            request_first_login(
                tenant_id=str(tenant_a), user_id="member-a", site_profile_id=str(profile_a),
                account_label="primary", vault_reference=str(vault_a),
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
        ) == "tenant-a-work"
        assert await conn.fetchval(
            "SELECT count(*) FROM authenticated_site_accounts a "
            "JOIN agent_permission_requests r ON r.id=a.credential_approval_request_id "
            "AND r.tenant_id=a.tenant_id WHERE a.tenant_id=$1",
            tenant_a,
        ) == 1
    finally:
        if pool is not None:
            await pool.close()
        await conn.close()


def test_account_approval_scope_real_postgres_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_exercise(monkeypatch))
