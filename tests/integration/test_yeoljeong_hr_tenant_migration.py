import os
import re
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations/20260919_yeoljeong_hr_tenant_isolation.sql"
ROLLBACK = ROOT / "migrations/20260919_yeoljeong_hr_tenant_isolation_rollback.sql"


def test_migration_has_reproducible_non_destructive_attribution_contract():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "yeoljeong_hr_tenant_attribution_audit" in sql
    assert "classification IN ('attributed', 'unresolved', 'conflict')" in sql
    assert "mapped_relational_business" in sql
    assert "mapped_explicit_fk" in sql
    assert "mapped_nonconflicting_legacy_payload" in sql
    assert "unique_authoritative_email" in sql
    assert "ambiguous_authoritative_email" in sql
    assert "DELETE FROM" not in sql.upper()
    assert "ON CONFLICT (ledger_table, row_id) DO UPDATE" in sql
    rollback = ROLLBACK.read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS yeoljeong_hr_tenant_attribution_audit" in rollback
    assert "DELETE FROM" not in rollback.upper()


@pytest.mark.asyncio
async def test_migration_requires_dedicated_postgresql(monkeypatch):
    """The Runner must inject an isolated disposable DB; shared PG* settings are not accepted."""
    url = os.getenv("YEOLJEONG_HR_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.fail(
            "YEOLJEONG_HR_TEST_DATABASE_URL is required for the destructive-safe temporary-schema "
            "migration/re-run/rollback integration test; refusing to use ambient PG* settings"
        )
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(url)
    schema = f"o2_hr_{uuid4().hex}"
    try:
        assert re.fullmatch(r"o2_hr_[0-9a-f]{32}", schema)
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute("""
            CREATE TABLE yeoljeong_business_tenant_mapping (business_id TEXT NOT NULL, tenant_id UUID NOT NULL);
            CREATE TABLE yeoljeong_employee_join_requests (
                id TEXT PRIMARY KEY, employee_email TEXT, business_id TEXT, request_payload JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(), deleted_at TIMESTAMPTZ);
            CREATE TABLE yeoljeong_onboarding_documents (
                id TEXT PRIMARY KEY, employee_request_id TEXT, employee_email TEXT, business_id TEXT,
                metadata JSONB DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(), deleted_at TIMESTAMPTZ);
            CREATE TABLE yeoljeong_contracts (
                id TEXT PRIMARY KEY, employee_email TEXT, business_id TEXT, contract_payload JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(), deleted_at TIMESTAMPTZ);
            CREATE TABLE yeoljeong_payroll_statements (
                id TEXT PRIMARY KEY, employee_email TEXT, business_id TEXT, statement_payload JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(), deleted_at TIMESTAMPTZ);
        """)
        tenant_a, tenant_b = uuid4(), uuid4()
        await conn.executemany(
            "INSERT INTO yeoljeong_business_tenant_mapping VALUES ($1, $2)",
            [("biz-a", tenant_a), ("biz-b", tenant_b)],
        )
        # Exercise a 60-row mixed-ledger fixture: 12/29/17/2. Exactly seven
        # have a safe one-time payload candidate; no PII is emitted by the audit.
        await conn.executemany(
            "INSERT INTO yeoljeong_employee_join_requests (id, employee_email, request_payload) VALUES ($1,$2,$3::jsonb)",
            [(f"j{i}", f"j{i}@example.invalid", '{"business_id":"biz-a"}' if i < 4 else "{}") for i in range(12)],
        )
        await conn.executemany(
            "INSERT INTO yeoljeong_onboarding_documents (id, employee_email, metadata) VALUES ($1,$2,$3::jsonb)",
            [(f"d{i}", f"d{i}@example.invalid", '{"business_id":"biz-a"}' if i < 3 else "{}") for i in range(29)],
        )
        await conn.executemany(
            "INSERT INTO yeoljeong_contracts (id, employee_email) VALUES ($1,$2)",
            [(f"c{i}", f"c{i}@example.invalid") for i in range(17)],
        )
        await conn.executemany(
            "INSERT INTO yeoljeong_payroll_statements (id, employee_email) VALUES ($1,$2)",
            [(f"p{i}", f"p{i}@example.invalid") for i in range(2)],
        )
        sql = MIGRATION.read_text(encoding="utf-8")
        await conn.execute(sql)
        counts = dict(await conn.fetch(
            "SELECT classification, count(*)::int count FROM yeoljeong_hr_tenant_attribution_audit GROUP BY classification"
        ))
        assert counts == {"attributed": 7, "unresolved": 53}
        await conn.execute(sql)
        assert await conn.fetchval("SELECT count(*) FROM yeoljeong_hr_tenant_attribution_audit") == 60

        # A mapped legacy signal conflicting with an authoritative relational
        # business must remain unassigned and reproducibly report conflict.
        await conn.execute(
            "INSERT INTO yeoljeong_contracts (id, employee_email, business_id, contract_payload) "
            "VALUES ('conflict', 'conflict@example.invalid', 'unmapped-business', '{\"business_id\":\"biz-b\"}')"
        )
        await conn.execute(sql)
        conflict = await conn.fetchrow(
            "SELECT classification, tenant_id FROM yeoljeong_hr_tenant_attribution_audit "
            "WHERE ledger_table='yeoljeong_contracts' AND row_id='conflict'"
        )
        assert dict(conflict) == {"classification": "conflict", "tenant_id": None}

        await conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        assert await conn.fetchval("SELECT count(*) FROM yeoljeong_contracts") == 18
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=$1 "
            "AND table_name='yeoljeong_contracts' AND column_name='tenant_id')", schema
        )
    finally:
        await conn.execute("RESET search_path")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
