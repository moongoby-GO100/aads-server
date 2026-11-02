"""Lease/cleanup contracts on disposable PostgreSQL, never the operational DB."""
import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import AsyncMock

import pytest

from app.services import gemini_web_usage as usage


@pytest.mark.parametrize('pool_size', [1, 2])
def test_database_lease_fencing_restart_cleanup_and_pool_capacity(monkeypatch, pool_size):
    dsn = os.getenv('AADS_GEMINI_USAGE_TEST_DATABASE_URL', '')
    if not dsn or not urlparse(dsn).path.removeprefix('/').endswith('_test'):
        pytest.skip('Requires disposable *_test AADS_GEMINI_USAGE_TEST_DATABASE_URL')
    asyncpg = pytest.importorskip('asyncpg')
    from app.core import db_pool
    root = Path(__file__).resolve().parents[2]

    async def scenario():
        schema = 'gemini_usage_' + uuid.uuid4().hex
        admin = await asyncpg.connect(dsn, timeout=5)
        pool = None
        try:
            await admin.execute(f'CREATE SCHEMA {schema}')
            await admin.execute(f'SET search_path TO {schema}, public')
            migration = (root / 'migrations/20261102_gemini_web_usage_bindings.sql').read_text()
            await admin.execute(migration)
            await admin.execute(migration)  # additive/idempotent, preserve rows
            pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=pool_size, command_timeout=3,
                server_settings={'search_path': f'{schema},public'})
            monkeypatch.setattr(db_pool, 'get_pool', lambda: pool)
            async with usage.operation_lease('collect') as token:
                assert token
                # Worker B cannot acquire even with a separate Collector/process state.
                async with usage.operation_lease('connect') as rival:
                    assert rival is None
                async with pool.acquire() as conn:
                    await usage.fence(conn, token)
                    await conn.execute("INSERT INTO gemini_web_usage_bindings "
                                       "(tenant_id,owner_email,owner_user_id,agent_id,account,session_id,endpoint) "
                                       "VALUES ('t','o','u','a','account','s','{\"kind\":\"local_agent\"}')")
                # Nested acquire during PC wait works even with a one-connection pool.
                monkeypatch.setattr(usage, 'registration_active', AsyncMock(return_value=True))
                monkeypatch.setattr(usage, 'account_enabled', AsyncMock(return_value=True))
                async def pc(*args):
                    async with pool.acquire() as conn:
                        assert await conn.fetchval('SELECT 1') == 1
                    return usage.unavailable('auth_required')
                c = usage.Collector()
                c._collect_page = pc
                # Current worker is already leased: a duplicate collector returns busy.
                assert (await c._collect('account', 'o', 't'))['status'] == 'busy'
            assert (await c._collect('account', 'o', 't'))['status'] == 'auth_required'

            async with usage.operation_lease('collect') as old:
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE gemini_web_usage_lease SET expires_at=now()-interval '1 second'")
                with pytest.raises(usage.LeaseExpired):
                    await usage.publish_sample(old, 't', 'o', 'account', usage.unavailable('stale'))
            async with pool.acquire() as conn:
                assert await conn.fetchval('SELECT state FROM gemini_web_usage_lease') == 'recovery_required'
                # Simulated process restart must retain the unresolved operation.
                assert usage.Collector().samples == {}
                await conn.execute("UPDATE gemini_web_usage_lease SET expires_at=now()-interval '181 seconds'")
            async with usage.operation_lease('cleanup') as newer:
                assert newer and newer != old
                with pytest.raises(usage.LeaseExpired):
                    await usage.publish_sample(old, 't', 'o', 'account', usage.unavailable('stale'))

            # Cancelled bootstrap keeps scope and blocks new work, including cleanup takeover.
            started = asyncio.Event()
            async def bootstrap():
                async with usage.operation_lease('connect', {'agent_id': 'a'}) as token:
                    assert token
                    started.set()
                    await asyncio.Event().wait()
            task = asyncio.create_task(bootstrap())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with usage.operation_lease('connect') as token:
                assert token is None
            async with usage.operation_lease('cleanup') as token:
                assert token is None
            async with pool.acquire() as conn:
                assert await conn.fetchval("SELECT scope->>'agent_id' FROM gemini_web_usage_lease") == 'a'
                await conn.execute("INSERT INTO gemini_web_usage_cleanup "
                                   "(request_id,tenant_id,owner_email,account) VALUES ('r','t','o','account')")
            await usage.defer_cleanup('r', 'browser_changed')
            await admin.execute((root / 'migrations/rollback/20261102_gemini_web_usage_bindings.down.sql').read_text())
            async with pool.acquire() as conn:
                row = await conn.fetchrow('SELECT * FROM gemini_web_usage_cleanup')
                assert row['state'] == 'retry_required' and row['attempts'] == 1
                assert row['work_key'] == 'extai-fixed-profile'
                assert await conn.fetchval('SELECT count(*) FROM gemini_web_usage_bindings') == 1
        finally:
            if pool:
                await pool.close()
            # Test-owned isolated schema only; application runtime never drops tables.
            await admin.execute(f'DROP SCHEMA {schema} CASCADE')
            await admin.close()
    asyncio.run(asyncio.wait_for(scenario(), timeout=25))
