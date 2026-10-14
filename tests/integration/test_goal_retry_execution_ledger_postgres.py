"""재작업 실행 장부를 실제 PostgreSQL 에서 검증한다 (T4/T5/T6/T10/T12/T14/T15).

이름이 `_test` 로 끝나는 일회용 DB 가 아니면 건너뛴다. 테스트마다 별도 스키마를 만들고 지운다.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest

ROOT = Path(__file__).parents[2]
TENANT_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
MS_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
MS_B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _dsn() -> str:
    url = os.environ.get("AADS_RETRY_LEDGER_TEST_DATABASE_URL") or os.environ.get("AADS_DRAFT_TEST_DATABASE_URL")
    database = urlparse(url).path.removeprefix("/") if url else os.environ.get("PGDATABASE", "")
    if not url or not database.endswith("_test"):
        pytest.skip("A disposable *_test PostgreSQL database URL is required")
    return url


async def _connect(dsn: str, schema: str | None = None):
    kwargs = {"server_settings": {"search_path": f"{schema}, public"}} if schema else {}
    return await asyncio.wait_for(asyncpg.connect(dsn, **kwargs), timeout=5)


async def _make_schema(dsn: str) -> str:
    schema = f"retry_ledger_{uuid.uuid4().hex}"
    conn = await _connect(dsn)
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute("""
            CREATE TABLE tenants (id uuid PRIMARY KEY);
            CREATE TABLE milestones (id uuid PRIMARY KEY, tenant_id uuid NOT NULL);
            CREATE TABLE pipeline_jobs (job_id text PRIMARY KEY, tenant_id uuid, instruction text);
            CREATE TABLE goal_task_links (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(), milestone_id uuid, task_id text,
                tenant_id uuid, created_at timestamptz DEFAULT now());
            CREATE TABLE chat_deferred_reactions (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(), session_id uuid NOT NULL,
                system_message text, dedupe_key text, status text NOT NULL DEFAULT 'pending',
                created_at timestamptz NOT NULL DEFAULT now());
        """)
        await conn.execute("INSERT INTO tenants VALUES ($1), ($2)", TENANT_A, TENANT_B)
        await conn.execute("INSERT INTO milestones VALUES ($1, $2), ($3, $4)", MS_A, TENANT_A, MS_B, TENANT_B)
        await conn.execute((ROOT / "migrations/20261014_goal_retry_execution_ledger.sql").read_text())
    finally:
        await conn.close()
    return schema


async def _drop_schema(dsn: str, schema: str) -> None:
    conn = await _connect(dsn)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    finally:
        await conn.close()


def _retry_instruction(link: str, milestone: uuid.UUID = MS_A) -> str:
    return f"GOAL_ID: g\nMILESTONE_ID: {milestone}\nRETRY_OF_LINK: {link}\nREMEDIATION_PURPOSE: failed_link_retry\n"


async def _job(conn, job_id: str, tenant: uuid.UUID, instruction: str) -> None:
    await conn.execute("INSERT INTO pipeline_jobs VALUES ($1, $2, $3)", job_id, tenant, instruction)


async def _charge(conn, job_id: str, cap: int = 2) -> dict:
    import json
    return json.loads(await conn.fetchval("SELECT charge_goal_retry_execution($1, $2)::text", job_id, cap))


def _run(coro_factory):
    async def runner():
        dsn = _dsn()
        schema = await _make_schema(dsn)
        try:
            await coro_factory(dsn, schema)
        finally:
            await _drop_schema(dsn, schema)
    asyncio.run(runner())


def test_charge_is_idempotent_capped_and_isolated_per_lineage_and_tenant():
    async def scenario(dsn, schema):
        conn = await _connect(dsn, schema)
        try:
            await _job(conn, "runner-aaaaaaa1", TENANT_A, "원래 실패 작업")
            await _job(conn, "runner-bbbbbbb1", TENANT_A, "다른 실패 작업")
            for i in (1, 2, 3):
                await _job(conn, f"runner-rework{i}", TENANT_A, _retry_instruction("runner-aaaaaaa1"))
            await _job(conn, "runner-other1", TENANT_A, _retry_instruction("runner-bbbbbbb1"))
            await _job(conn, "runner-plain1", TENANT_A, "재작업 아님 — 본문에 RETRY_OF_LINK: runner-aaaaaaa1 언급")
            # 같은 id 패턴이 다른 테넌트에도 있다 (T10)
            await _job(conn, "runner-tenantb", TENANT_B, _retry_instruction("runner-aaaaaaa1", MS_B))

            assert (await _charge(conn, "runner-plain1"))["outcome"] == "not_retry"
            first = await _charge(conn, "runner-rework1")
            assert first["outcome"] == "charged" and first["slot"] == 1  # T4
            again = await _charge(conn, "runner-rework1")
            assert again["outcome"] == "already_charged"  # T5
            assert (await _charge(conn, "runner-rework2"))["slot"] == 2
            blocked = await _charge(conn, "runner-rework3")
            assert blocked["outcome"] == "cap_exceeded" and blocked["used"] == 2  # 3번째는 사전 차단
            assert (await _charge(conn, "runner-rework3", 99))["outcome"] == "cap_exceeded"  # T14
            assert (await _charge(conn, "runner-other1"))["outcome"] == "charged"  # T6: 다른 계보는 독립
            assert (await _charge(conn, "runner-tenantb"))["outcome"] == "charged"  # T10: 다른 테넌트 독립
            rows = await conn.fetch("SELECT tenant_id, count(*) AS n FROM goal_retry_executions GROUP BY tenant_id")
            assert {r["tenant_id"]: r["n"] for r in rows} == {TENANT_A: 3, TENANT_B: 1}
            assert await conn.fetchval("SELECT count(*) FROM goal_retry_executions WHERE job_id = 'runner-rework3'") == 0
        finally:
            await conn.close()
    _run(scenario)


def test_tenant_and_scope_failures_fail_closed():
    async def scenario(dsn, schema):
        conn = await _connect(dsn, schema)
        try:
            await _job(conn, "runner-notenant", None, _retry_instruction("runner-aaaaaaa1"))
            await _job(conn, "runner-foreignms", TENANT_A, _retry_instruction("runner-aaaaaaa1", MS_B))
            await _job(conn, "runner-noms", TENANT_A, "RETRY_OF_LINK: runner-aaaaaaa1\n")
            assert (await _charge(conn, "runner-missing"))["outcome"] == "job_not_found"
            assert (await _charge(conn, "runner-notenant"))["outcome"] == "tenant_missing"
            assert (await _charge(conn, "runner-foreignms"))["outcome"] == "scope_mismatch"
            assert (await _charge(conn, "runner-noms"))["outcome"] == "milestone_missing"
            assert await conn.fetchval("SELECT count(*) FROM goal_retry_executions") == 0
        finally:
            await conn.close()
    _run(scenario)


def test_rework_of_a_rework_shares_the_root_lineage():
    async def scenario(dsn, schema):
        conn = await _connect(dsn, schema)
        try:
            await _job(conn, "runner-root0001", TENANT_A, "원래 실패")
            await _job(conn, "runner-chain001", TENANT_A, _retry_instruction("runner-root0001"))
            await _job(conn, "runner-chain002", TENANT_A, _retry_instruction("runner-chain001"))
            await _job(conn, "runner-chain003", TENANT_A, _retry_instruction("runner-chain002"))
            assert (await _charge(conn, "runner-chain001"))["lineage"] == "runner-root0001"
            assert (await _charge(conn, "runner-chain002"))["lineage"] == "runner-root0001"
            assert (await _charge(conn, "runner-chain003"))["outcome"] == "cap_exceeded"
        finally:
            await conn.close()
    _run(scenario)


def test_concurrent_charges_never_exceed_the_cap():
    async def scenario(dsn, schema):
        setup = await _connect(dsn, schema)
        try:
            await _job(setup, "runner-root0002", TENANT_A, "원래 실패")
            ids = [f"runner-race{i:04d}" for i in range(12)]
            for job_id in ids:
                await _job(setup, job_id, TENANT_A, _retry_instruction("runner-root0002"))
        finally:
            await setup.close()

        async def one(job_id):
            conn = await _connect(dsn, schema)
            try:
                return await _charge(conn, job_id)
            finally:
                await conn.close()

        # 같은 job 을 여러 번 + 서로 다른 job 을 동시에 (T5 + T15)
        results = await asyncio.gather(*[one(j) for j in ids for _ in range(2)])
        outcomes = [r["outcome"] for r in results]
        assert outcomes.count("charged") == 2
        check = await _connect(dsn, schema)
        try:
            assert await check.fetchval("SELECT count(*) FROM goal_retry_executions") == 2
            assert await check.fetchval("SELECT count(DISTINCT job_id) FROM goal_retry_executions") == 2
        finally:
            await check.close()
    _run(scenario)


def test_unique_constraints_reject_direct_overcharge():
    async def scenario(dsn, schema):
        conn = await _connect(dsn, schema)
        try:
            await conn.execute(
                "INSERT INTO goal_retry_executions (tenant_id, milestone_id, lineage, job_id, slot) "
                "VALUES ($1, $2, 'L', 'j1', 1), ($1, $2, 'L', 'j2', 2)", TENANT_A, MS_A)
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO goal_retry_executions (tenant_id, milestone_id, lineage, job_id, slot) "
                    "VALUES ($1, $2, 'L', 'j3', 2)", TENANT_A, MS_A)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO goal_retry_executions (tenant_id, milestone_id, lineage, job_id, slot) "
                    "VALUES ($1, $2, 'L', 'j4', 3)", TENANT_A, MS_A)
        finally:
            await conn.close()
    _run(scenario)


def test_windowed_enqueue_never_reuses_a_terminal_queue(monkeypatch):
    from app.services import chat_service

    async def scenario(dsn, schema):
        conn = await _connect(dsn, schema)
        session = uuid.uuid4()

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield conn

        monkeypatch.setattr(chat_service, "get_pool", lambda: Pool())
        monkeypatch.setattr(chat_service, "_safe_reaction_message", lambda _sid, message: message)
        try:
            first = await chat_service.enqueue_next_step_reaction(
                str(session), "본문", dedupe_key="next_step:auto:k", reuse_seconds=900)
            assert first["created"] is True
            live = await chat_service.enqueue_next_step_reaction(
                str(session), "본문", dedupe_key="next_step:auto:k", reuse_seconds=900)
            assert live["created"] is False and live["queue_id"] == first["queue_id"]

            for dead in ("skipped_stale", "failed", "cancelled"):  # T2/T12
                await conn.execute("UPDATE chat_deferred_reactions SET status = $1 WHERE id = $2::uuid",
                                   dead, first["queue_id"])
                fresh = await chat_service.enqueue_next_step_reaction(
                    str(session), "본문", dedupe_key="next_step:auto:k", reuse_seconds=900)
                assert fresh["created"] is True and fresh["queue_id"] != first["queue_id"]
                await conn.execute("UPDATE chat_deferred_reactions SET status = 'completed' WHERE id = $1::uuid",
                                   fresh["queue_id"])
                first = fresh
            # 승인 배치(reuse_seconds=0) 의 무기한 창 의미는 그대로다
            batch = await chat_service.enqueue_next_step_reaction(
                str(session), "본문", dedupe_key="next_step:batch:k")
            await conn.execute("UPDATE chat_deferred_reactions SET status = 'failed' WHERE id = $1::uuid",
                               batch["queue_id"])
            again = await chat_service.enqueue_next_step_reaction(
                str(session), "본문", dedupe_key="next_step:batch:k")
            assert again["created"] is False and again["queue_id"] == batch["queue_id"]
        finally:
            await conn.close()
    _run(scenario)
