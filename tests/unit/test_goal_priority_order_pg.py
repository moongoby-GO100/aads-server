"""goals.priority_order 의 실제 PostgreSQL 검증 (초기값 멱등 · reorder 원자성 · 등급→순서 정렬).

운영 DB 는 건드리지 않는다. GOAL_PRIORITY_ORDER_TEST_ADMIN_URL(CREATEDB 가능한 접속 URL)로 일회용 DB 를 만들고
끝나면 DROP 한다. 환경변수가 없으면 skip 된다(test_goal_grant_pg_semantics.py 와 같은 방식).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

from app.services import goal_priority_order as po
from app.services.goal_signals import priority_rank_case_sql

ADMIN_URL = os.getenv("GOAL_PRIORITY_ORDER_TEST_ADMIN_URL", "")
pytestmark = pytest.mark.skipif(
    not ADMIN_URL, reason="GOAL_PRIORITY_ORDER_TEST_ADMIN_URL (CREATEDB 가능한 접속 URL) 필요")

T1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
T2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SCHEMA = """
CREATE TABLE goals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id UUID NOT NULL,
    title TEXT, priority TEXT, status TEXT NOT NULL DEFAULT 'active',
    priority_order INTEGER, created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ DEFAULT now());
"""
LOOP = asyncio.new_event_loop()


def run(coro):
    return LOOP.run_until_complete(coro)


@pytest.fixture
def conn():
    import asyncpg

    name = "goal_po_pg_" + uuid.uuid4().hex[:10]
    url = urlunsplit(urlsplit(ADMIN_URL)._replace(path="/" + name))

    async def create():
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute('CREATE DATABASE "%s"' % name)
        finally:
            await admin.close()
        c = await asyncpg.connect(url)
        await c.execute(SCHEMA)
        return c

    c = run(create())
    yield c

    async def drop():
        await c.close()
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
        finally:
            await admin.close()

    run(drop())


async def _seed(c, tenant, title, priority, status="active"):
    return await c.fetchval(
        "INSERT INTO goals (tenant_id, title, priority, status) VALUES ($1::uuid,$2,$3,$4) RETURNING id::text",
        tenant, title, priority, status)


def test_backfill_orders_by_rank_then_created_and_is_idempotent(conn) -> None:
    async def scenario():
        p2 = await _seed(conn, T1, "p2", "P2")
        p0 = await _seed(conn, T1, "p0", "P0")
        p1a = await _seed(conn, T1, "p1a", "P1")
        p1b = await _seed(conn, T1, "p1b", "P1")
        done = await _seed(conn, T1, "done", "P0", "completed")
        other = await _seed(conn, T2, "other", "P3")
        await po.backfill_priority_order(conn)
        first = {r["id"]: r["priority_order"] for r in await conn.fetch("SELECT id::text, priority_order FROM goals")}
        await po.backfill_priority_order(conn)
        second = {r["id"]: r["priority_order"] for r in await conn.fetch("SELECT id::text, priority_order FROM goals")}
        return (p0, p1a, p1b, p2, done, other), first, second

    (p0, p1a, p1b, p2, done, other), first, second = run(scenario())
    assert [first[g] for g in (p0, p1a, p1b, p2)] == [1, 2, 3, 4]
    assert first[done] is None
    assert first[other] == 1
    assert first == second


def test_backfill_appends_after_existing_values(conn) -> None:
    async def scenario():
        kept = await _seed(conn, T1, "kept", "P3")
        await conn.execute("UPDATE goals SET priority_order = 5 WHERE id = $1::uuid", kept)
        late = await _seed(conn, T1, "late", "P0")
        await po.backfill_priority_order(conn)
        return kept, late, {r["id"]: r["priority_order"] for r in await conn.fetch("SELECT id::text, priority_order FROM goals")}

    kept, late, orders = run(scenario())
    assert orders[kept] == 5
    assert orders[late] == 6


def test_reorder_is_atomic_and_tenant_scoped(conn) -> None:
    async def scenario():
        a, b = await _seed(conn, T1, "a", "P1"), await _seed(conn, T1, "b", "P1")
        foreign = await _seed(conn, T2, "f", "P1")
        n = await po.reorder_goals(conn, T1, [b, a])
        got = {r["id"]: r["priority_order"] for r in await conn.fetch("SELECT id::text, priority_order FROM goals")}
        errors = []
        for ids in ([a, foreign], [a, a]):
            try:
                await po.reorder_goals(conn, T1, ids)
            except po.ReorderError as exc:
                errors.append(exc.status_code)
        after = {r["id"]: r["priority_order"] for r in await conn.fetch("SELECT id::text, priority_order FROM goals")}
        return n, got, errors, after, (a, b, foreign)

    n, got, errors, after, (a, b, foreign) = run(scenario())
    assert n == 2 and (got[b], got[a]) == (1, 2)
    assert errors == [404, 422]
    assert after == got and after[foreign] is None


def test_sort_expression_orders_grade_then_order_nulls_last(conn) -> None:
    async def scenario():
        rows = {}
        for title, prio, order in [("p2-1", "P2", 1), ("p0-9", "P0", 9), ("p1-null", "P1", None), ("p1-2", "P1", 2), ("p1-1", "P1", 1)]:
            gid = await _seed(conn, T1, title, prio)
            await conn.execute("UPDATE goals SET priority_order = $2 WHERE id = $1::uuid", gid, order)
            rows[gid] = title
        sql = (f"SELECT title FROM goals g ORDER BY {priority_rank_case_sql('g.priority')}, "
               "g.priority_order ASC NULLS LAST")
        plan = await conn.fetch("EXPLAIN " + sql)
        return [r["title"] for r in await conn.fetch(sql)], plan

    titles, plan = run(scenario())
    assert titles == ["p0-9", "p1-1", "p1-2", "p1-null", "p2-1"]
    assert plan
