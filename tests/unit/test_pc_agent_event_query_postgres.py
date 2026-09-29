"""Execute the status SQL on disposable PostgreSQL (never the service database).

Set PC_AGENT_TEST_POSTGRES_DSN to an isolated test database. This module creates
only a connection-local temporary table and rolls it away when the pool closes.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_event_query_fences_connections_and_preserves_legacy_offline(monkeypatch):
    dsn = os.getenv("PC_AGENT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires disposable PostgreSQL via PC_AGENT_TEST_POSTGRES_DSN")
    import asyncpg
    from app.api import pc_agent as api
    from app.core import db_pool

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    monkeypatch.setattr(db_pool, "get_pool", lambda: pool)
    try:
        async with pool.acquire() as conn:
            await conn.execute("""CREATE TEMP TABLE pc_agent_connection_events (
                id bigserial PRIMARY KEY, agent_id text, event text, reason text,
                metadata jsonb, created_at timestamptz)
            """)
            now = datetime.now(timezone.utc)

            async def event(agent, name, metadata, age=0):
                await conn.execute("""INSERT INTO pc_agent_connection_events
                    (agent_id,event,reason,metadata,created_at) VALUES ($1,$2,'',$3,$4)""",
                    agent, name, json.dumps(metadata), now - timedelta(days=age))

            # Replacement cannot inherit the old owner's observations or accept
            # heartbeat-supplied ownership. Same timestamps also exercise id order.
            await event("replacement", "connected", {"user_id": "alice", "connection_id": "a"})
            await event("replacement", "connected", {"user_id": "bob", "connection_id": "b"})
            await event("replacement", "disconnected", {"connection_id": "a", "last_observation": {"cpu_percent": 99}})
            await event("replacement", "heartbeat_status", {"connection_id": "b", "user_id": "mallory", "last_observation": {"cpu_percent": 12}})
            await event("failed", "connected", {"user_id": "alice", "connection_id": "old"})
            await event("failed", "socket_connected", {"user_id": "alice", "connection_id": "retry"})
            await event("failed", "register_failed", {"connection_id": "retry"})
            await event("failed", "disconnected", {"connection_id": "old"})
            # Legacy identity can be older than the requested activity window.
            await event("legacy", "connected", {"user_id": "alice"}, age=30)
            await event("legacy", "disconnected", {"last_observation": {"cpu_percent": 45}})
            await event("historical", "connected", {"user_id": "alice", "connection_id": "h"}, age=30)
            await event("historical", "heartbeat_status", {"connection_id": "h"})
            await event("unknown", "disconnected", {"user_id": "mallory"})
            await event("ownerless", "connected", {"user_id": "alice", "connection_id": "owned"})
            await event("ownerless", "socket_connected", {"user_id": "", "connection_id": "anon"})
            await event("ownerless", "register_failed", {"connection_id": "anon"})
        bob = await api._latest_known_pc_agents_from_events("bob", include_all_agents=False)
        assert len(bob) == 1
        assert bob[0]["last_observation"]["cpu_percent"] == 12
        assert await api._latest_known_pc_agents_from_events("mallory", include_all_agents=False) == []
        alice = {row["agent_id"]: row for row in await api._latest_known_pc_agents_from_events("alice", include_all_agents=False)}
        assert set(alice) == {"failed", "legacy", "historical"}
        assert alice["failed"]["last_event"] == "register_failed"
        assert alice["legacy"]["last_event"] == "disconnected"
        assert alice["legacy"]["last_observation"]["cpu_percent"] == 45
        admin = {row["agent_id"]: row for row in await api._latest_known_pc_agents_from_events("", include_all_agents=True)}
        assert set(admin) == {"replacement", "failed", "legacy", "historical", "unknown", "ownerless"}
        assert admin["unknown"]["user_id"] == ""
        assert admin["ownerless"]["user_id"] == ""
        assert admin["ownerless"]["last_observation"] is None
    finally:
        await pool.close()
