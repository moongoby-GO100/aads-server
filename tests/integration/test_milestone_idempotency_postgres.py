"""Only an explicitly provided isolated *_test DB may be used; never DATABASE_URL."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.services.goal_manager import GoalStateMachine
from app.services.milestone_idempotency import (
    MilestoneConflict, MilestoneGoalNotFound, create_milestone,
)

TENANT, OTHER_TENANT, GOAL, OTHER_GOAL = [str(uuid4()) for _ in range(4)]
PAYLOAD = dict(title="C0", sequence=1, completion_criteria=None, auto_advance=True,
               owner_session_id=None, owner_role_key=None)


class IndependentConnections:
    def __init__(self, url, schema):
        self.url, self.schema = url, schema

    @asynccontextmanager
    async def acquire(self):
        conn = await connect_test_database(self.url)
        try:
            await conn.execute(f'SET search_path TO "{self.schema}"')
            await conn.execute("SET statement_timeout = '10s'")
            yield conn
        finally:
            await conn.close()


def resolve_test_database_url():
    """Reject missing/unsafe configuration before any connection or schema writes."""
    url = os.environ.get("AADS_MILESTONE_TEST_DATABASE_URL")
    try:
        parsed = urlparse(url or "")
        allowed = (parsed.scheme in {"postgres", "postgresql"}
                   and bool(parsed.hostname) and not parsed.query and not parsed.fragment
                   and parsed.path.removeprefix("/").endswith("_test")
                   and "/" not in parsed.path.removeprefix("/"))
    except ValueError:
        allowed = False
    if not url or not allowed:
        message = "AADS_MILESTONE_TEST_DATABASE_URL must name an isolated *_test PostgreSQL database"
        if os.environ.get("AADS_MILESTONE_DB_REQUIRED") == "1":
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    return url


async def connect_test_database(url):
    # Never include connection exceptions: they may contain credentials or the DSN.
    try:
        return await asyncpg.connect(url, timeout=5)
    except Exception:
        pytest.fail("Milestone test database connection failed (connection details redacted)", pytrace=False)


async def exercise():
    url = resolve_test_database_url()
    admin = await connect_test_database(url)
    schema = "milestone_idempotency_test_" + uuid4().hex
    outcome = "failed"
    created = False
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        await admin.execute(f'SET search_path TO "{schema}"')
        await admin.execute("CREATE TABLE goals (id uuid PRIMARY KEY, tenant_id uuid NOT NULL)")
        await admin.execute("""CREATE TABLE milestones (
            id uuid PRIMARY KEY, goal_id uuid NOT NULL REFERENCES goals(id), tenant_id uuid NOT NULL,
            title text NOT NULL CHECK (title <> 'FAIL'), sequence_order integer,
            completion_criteria text, auto_advance boolean, status text,
            owner_session_id uuid, owner_role_key text)""")
        await admin.execute("INSERT INTO goals VALUES ($1::uuid,$2::uuid),($3::uuid,$2::uuid)",
                            GOAL, TENANT, OTHER_GOAL)
        migration = (Path(__file__).parents[2] / "migrations/20261103_milestone_idempotency.sql").read_text()
        await admin.execute(migration)
        await admin.execute(migration)  # repeatable additive installation
        pool = IndependentConnections(url, schema)

        async def create(key="same", payload=None, tenant=TENANT, goal=GOAL):
            return await create_milestone(pool, tenant_id=tenant, goal_id=goal,
                                          key=key, payload=PAYLOAD if payload is None else payload)

        # Each request acquires a new physical PostgreSQL connection.
        results = await asyncio.gather(*(create() for _ in range(8)))
        assert all(row == results[0] for row in results)
        assert await admin.fetchval("SELECT count(*) FROM milestones") == 1
        with pytest.raises(MilestoneConflict):
            await create(payload={**PAYLOAD, "title": "changed"})
        race = await asyncio.gather(create("race"), create("race", {**PAYLOAD, "sequence": 2}),
                                    return_exceptions=True)
        assert sum(isinstance(row, MilestoneConflict) for row in race) == 1
        assert sum(isinstance(row, dict) for row in race) == 1

        # Replay is the stored original response even after live status/owner changes.
        await admin.execute("UPDATE milestones SET status='completed', owner_role_key='changed'")
        assert await create() == results[0]
        with pytest.raises(MilestoneGoalNotFound):
            await create(tenant=OTHER_TENANT)
        other = await create(goal=OTHER_GOAL)
        assert other["milestone_id"] != results[0]["milestone_id"]
        await admin.execute("INSERT INTO goals VALUES ($1::uuid,$2::uuid)", str(uuid4()), OTHER_TENANT)
        foreign_goal = await admin.fetchval("SELECT id FROM goals WHERE tenant_id=$1::uuid", OTHER_TENANT)
        isolated = await create(tenant=OTHER_TENANT, goal=str(foreign_goal))
        assert isolated["milestone_id"] != results[0]["milestone_id"]

        # Insert failure rolls back the reservation too: corrected retry may reserve it.
        with pytest.raises(asyncpg.CheckViolationError):
            await create("rollback", {**PAYLOAD, "title": "FAIL"})
        assert await admin.fetchval("SELECT count(*) FROM milestone_idempotency WHERE idempotency_key='rollback'") == 0
        retry = await create("rollback")
        assert retry == await create("rollback")
        # External transaction rollback must also remove both records.
        class OuterTransaction:
            @asynccontextmanager
            async def acquire(self):
                yield admin
        transaction = admin.transaction()
        await transaction.start()
        await create_milestone(OuterTransaction(), tenant_id=TENANT, goal_id=GOAL,
                               key="outer-rollback", payload=PAYLOAD)
        await transaction.rollback()
        assert await admin.fetchval("SELECT count(*) FROM milestone_idempotency WHERE idempotency_key='outer-rollback'") == 0

        # Legacy internal callers and authenticated no-key callers stay non-idempotent.
        machine = GoalStateMachine()
        machine._pool = AsyncMock(return_value=pool)
        machine._trace = AsyncMock()
        legacy = await machine.add_milestone(GOAL, "legacy", 3)
        unkeyed = await machine.add_milestone(GOAL, "legacy", 3, tenant_id=TENANT)
        assert legacy["milestone_id"] != unkeyed["milestone_id"]
        assert str(await admin.fetchval("SELECT tenant_id FROM milestones WHERE id=$1::uuid",
                                       unkeyed["milestone_id"])) == TENANT
        with pytest.raises(MilestoneGoalNotFound):
            await machine.add_milestone(GOAL, "foreign", 3, tenant_id=OTHER_TENANT)

        # Deferred FK prevents a committed key without a matching milestone/scope.
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with admin.transaction():
                await admin.execute("""INSERT INTO milestone_idempotency
                    (tenant_id,goal_id,idempotency_key,payload_fingerprint,milestone_id,response)
                    VALUES ($1::uuid,$2::uuid,'orphan',$3,$4::uuid,jsonb_build_object('milestone_id',$4::text))""",
                    TENANT, GOAL, "0" * 64, str(uuid4()))
        assert await admin.fetchval("""SELECT count(*) FROM milestone_idempotency k
            LEFT JOIN milestones m ON m.id=k.milestone_id AND m.goal_id=k.goal_id AND m.tenant_id=k.tenant_id
            WHERE m.id IS NULL""") == 0
        await admin.execute("DELETE FROM milestones WHERE id=$1::uuid", retry["milestone_id"])
        assert await admin.fetchval("SELECT count(*) FROM milestone_idempotency WHERE idempotency_key='rollback'") == 0
        outcome = "passed"
    finally:
        if created:
            print(f"milestone_test_schema={schema} result={outcome} preserved=true", flush=True)
        await admin.close()


def test_postgres_concurrency_rollback_and_isolation():
    asyncio.run(exercise())
