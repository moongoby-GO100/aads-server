"""Opt-in PostgreSQL session-lock checks; no application rows or provider calls.

AADS_TEST_DATABASE_URL must point to the verified primary. Uses only registry's
advisory lock, released by finally/connection close, and a stub sync body.
"""
import asyncio
import os
from unittest.mock import AsyncMock

import asyncpg
import pytest

from app.services import model_registry

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    not os.getenv("AADS_TEST_DATABASE_URL"), reason="explicit primary DSN required")]


async def test_sync_waits_for_another_database_session_before_reading_state(monkeypatch):
    pool = await asyncpg.create_pool(os.environ["AADS_TEST_DATABASE_URL"], min_size=2, max_size=3)
    holder = await pool.acquire()
    body = AsyncMock(return_value={"ok": True})
    task = None
    try:
        assert await holder.fetchval("SELECT pg_try_advisory_lock($1)", model_registry._SYNC_LOCK_KEY)
        monkeypatch.setattr(model_registry, "get_pool", lambda: pool)
        monkeypatch.setattr(model_registry, "_sync_local_lock", asyncio.Lock())
        monkeypatch.setattr(model_registry, "_sync_model_registry_locked", body)
        task = asyncio.create_task(model_registry.sync_model_registry(reason="lock-integration"))
        await asyncio.sleep(0.15)
        assert not task.done()
        body.assert_not_awaited()
        await holder.execute("SELECT pg_advisory_unlock($1)", model_registry._SYNC_LOCK_KEY)
        assert (await asyncio.wait_for(task, 3))["ok"]
        body.assert_awaited_once()
    finally:
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await holder.execute("SELECT pg_advisory_unlock_all()")
        await pool.release(holder)
        await pool.close()


async def test_cancellation_releases_advisory_ownership(monkeypatch):
    pool = await asyncpg.create_pool(os.environ["AADS_TEST_DATABASE_URL"], min_size=2, max_size=3)
    entered = asyncio.Event()

    async def body(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(model_registry, "get_pool", lambda: pool)
    monkeypatch.setattr(model_registry, "_sync_local_lock", asyncio.Lock())
    monkeypatch.setattr(model_registry, "_sync_model_registry_locked", body)
    task = asyncio.create_task(model_registry.sync_model_registry(reason="cancel-integration"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with pool.acquire() as other:
            assert not await other.fetchval("SELECT pg_try_advisory_lock($1)", model_registry._SYNC_LOCK_KEY)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert await other.fetchval("SELECT pg_try_advisory_lock($1)", model_registry._SYNC_LOCK_KEY)
            await other.execute("SELECT pg_advisory_unlock($1)", model_registry._SYNC_LOCK_KEY)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await pool.close()
