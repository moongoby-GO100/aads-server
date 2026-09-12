"""Opt-in PostgreSQL integration coverage for the WP04 read-only contract.

Set ``AADS_WP04_TEST_DATABASE_URL`` to a disposable test database. Only
connection-local temporary tables are created; the public schema is untouched.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.services import chat_read_model

asyncpg = pytest.importorskip("asyncpg")


class _AcquireConnection:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _BoundPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AcquireConnection(self.connection)


@pytest.mark.asyncio
async def test_real_postgres_changes_query_runs_in_read_only_transaction(monkeypatch):
    """T11/T35: a real connection reports read-only and serves a scoped delta."""
    database_url = os.getenv("AADS_WP04_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("AADS_WP04_TEST_DATABASE_URL is not configured")

    connection = await asyncpg.connect(database_url)
    tenant_id, session_id, event_id = uuid4(), uuid4(), uuid4()
    try:
        await connection.execute(
            """
            CREATE TEMP TABLE chat_sessions (
                id uuid PRIMARY KEY, tenant_id uuid NOT NULL, user_id text
            );
            CREATE TEMP TABLE chat_session_revisions (
                tenant_id uuid NOT NULL, session_id uuid NOT NULL,
                revision bigint NOT NULL, message_revision bigint NOT NULL,
                artifact_revision bigint NOT NULL, execution_revision bigint NOT NULL,
                PRIMARY KEY (tenant_id, session_id)
            );
            CREATE TEMP TABLE chat_outbox (
                event_id uuid PRIMARY KEY, tenant_id uuid NOT NULL, session_id uuid NOT NULL,
                session_revision bigint NOT NULL, event_type text NOT NULL,
                payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
            );
            CREATE TEMP TABLE chat_execution_checkpoints (execution_id uuid PRIMARY KEY);
            CREATE TEMP TABLE chat_messages (content_version bigint NOT NULL DEFAULT 1);
            SET search_path TO pg_temp;
            """
        )
        await connection.execute(
            "INSERT INTO chat_sessions VALUES ($1, $2, 'subject-42')",
            session_id,
            tenant_id,
        )
        await connection.execute(
            "INSERT INTO chat_session_revisions VALUES ($1, $2, 1, 1, 0, 0)",
            tenant_id,
            session_id,
        )
        await connection.execute(
            """
            INSERT INTO chat_outbox (
                event_id, tenant_id, session_id, session_revision, event_type, payload
            ) VALUES ($1, $2, $3, 1, 'message.changed', $4::jsonb)
            """,
            event_id,
            tenant_id,
            session_id,
            '{"message_id":"00000000-0000-4000-8000-000000000999",'
            '"operation":"UPDATE","tombstone":false}',
        )
        async with chat_read_model._read_only_transaction(connection):
            assert await connection.fetchval("SHOW transaction_read_only") == "on"

        monkeypatch.setattr(chat_read_model, "_pool", lambda: _BoundPool(connection))
        result = await chat_read_model.get_changes_v2(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id="subject-42",
            after_revision="0",
        )

        assert result["current_revision"] == "1"
        assert result["next_after_revision"] == "1"
        assert result["changed_message_ids"] == [
            "00000000-0000-4000-8000-000000000999"
        ]
        assert result["snapshot_required"] is False
    finally:
        await connection.close()
