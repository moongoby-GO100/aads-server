"""mockup_gate_decisions on a real PostgreSQL: migration, idempotency, rollback, and the recorder's SQL.

Set AADS_MOCKUP_TEST_DATABASE_URL to a disposable database whose name contains "test". Everything runs in a
throw-away schema that is dropped afterwards, so no existing table is touched. Without the variable the tests skip
(AADS_CANONICAL_DB_REQUIRED=1 turns an unreachable database into a failure).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio


ROOT = Path(__file__).resolve().parents[2]
UP = (ROOT / "migrations" / "20261014_mockup_gate_decisions.sql").read_text(encoding="utf-8")
DOWN = (ROOT / "migrations" / "rollback" / "20261014_mockup_gate_decisions.down.sql").read_text(encoding="utf-8")
TENANT = "33333333-3333-4333-8333-333333333333"


@pytest_asyncio.fixture
async def conn():
    dsn = os.getenv("AADS_MOCKUP_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("AADS_MOCKUP_TEST_DATABASE_URL not set")
    assert "test" in urlsplit(dsn).path.lower(), "refusing to run against a non-test database"
    try:
        connection = await asyncpg.connect(dsn, timeout=3)
    except (OSError, asyncpg.PostgresError) as exc:
        if os.getenv("AADS_CANONICAL_DB_REQUIRED") == "1":
            pytest.fail(str(exc))
        pytest.skip(f"test database unreachable: {exc}")
    global svc
    from app.services import mockup_review_service as svc  # imported late: it needs JWT_SECRET_KEY like the app

    schema = f"gate_{uuid4().hex[:12]}"
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    await connection.execute(f'SET search_path TO "{schema}"')
    try:
        yield connection
    finally:
        await connection.execute("SET search_path TO public")
        await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await connection.close()


def verdict(**over):
    base = {"phase": "chat_write", "mode": "shadow", "allowed": True, "blocking": False, "shadow": True,
            "reasons": ["mockup_bundle_required"], "ui_reasons": ["ui_file:src/components/Panel.tsx"], "declared": False}
    return {**base, **over}


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_rolls_back(conn):
    await conn.execute(UP)
    await conn.execute(UP)
    assert await conn.fetchval("SELECT to_regclass('mockup_gate_decisions') IS NOT NULL")
    await conn.execute(DOWN)
    assert not await conn.fetchval("SELECT to_regclass('mockup_gate_decisions') IS NOT NULL")
    await conn.execute(DOWN)


@pytest.mark.asyncio
async def test_every_verdict_is_stored_with_a_kst_time(conn):
    await conn.execute(UP)
    for phase in ("submit", "pre_execution", "checkpoint", "chat_write", "pre_push"):
        assert await svc.record_gate_decision(conn, result=verdict(phase=phase), tenant=TENANT, project="AADS",
                                              task_id=f"push-{phase}", actor="tester")
    assert await svc.record_gate_decision(
        conn, result=verdict(phase="submit", mode="enforce", allowed=False, blocking=True, shadow=False, declared=True,
                             reasons=["approval_revoked"], review_id=str(uuid4()), revision_id=str(uuid4()),
                             audit_event_id=5),
        tenant=TENANT, project="AADS", task_id="runner-ab12cd34", actor="u1")
    rows = await conn.fetch("SELECT * FROM mockup_gate_decisions ORDER BY id")
    assert [r["phase"] for r in rows] == ["submit", "pre_execution", "checkpoint", "chat_write", "pre_push", "submit"]
    last = rows[-1]
    assert (last["mode"], last["allowed"], last["blocking"], last["bundle_declared"], last["job_id"],
            last["audit_event_id"], str(last["tenant_id"])) == ("enforce", False, True, True, "runner-ab12cd34", 5, TENANT)
    assert json.loads(last["reasons"]) == ["approval_revoked"] and rows[0]["job_id"] is None
    kst = await conn.fetchval("SELECT decided_at_kst - (decided_at AT TIME ZONE 'UTC') FROM mockup_gate_decisions LIMIT 1")
    assert kst.total_seconds() == 9 * 3600


@pytest.mark.asyncio
async def test_check_constraints_reject_unknown_phase_and_mode(conn):
    await conn.execute(UP)
    for bad in ({"phase": "deploy"}, {"mode": "block"}):
        assert await svc.record_gate_decision(conn, result=verdict(**bad), tenant=TENANT, project="AADS",
                                              task_id="t", actor="a") is False
    assert await conn.fetchval("SELECT count(*) FROM mockup_gate_decisions") == 0


@pytest.mark.asyncio
async def test_a_missing_table_does_not_poison_the_callers_transaction(conn):
    async with conn.transaction():
        assert await svc.record_gate_decision(conn, result=verdict(), tenant=TENANT, project="AADS", task_id="t",
                                              actor="a") is False
        assert await conn.fetchval("SELECT 1") == 1  # the surrounding transaction is still usable
