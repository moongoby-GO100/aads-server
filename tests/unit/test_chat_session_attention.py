from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.services import chat_service


TENANT_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "attention-test-user"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def test_list_session_attention_is_user_scoped_and_counts_states(monkeypatch):
    now = datetime.now(timezone.utc)
    working_session = uuid4()
    completed_session = uuid4()

    class FakeConn:
        async def fetchval(self, query, *args):
            assert "ON CONFLICT (tenant_id, user_id)" in query
            assert args == (UUID(TENANT_ID), USER_ID)
            return now

        async def fetch(self, query, *args):
            assert "s.tenant_id = $1" in query
            assert "ack.user_id = $2" in query
            assert args == (UUID(TENANT_ID), USER_ID, now)
            base = {
                "workspace_id": uuid4(),
                "workspace_name": "[AADS] 프로젝트 매니저",
                "workspace_icon": "A",
                "project_key": "AADS",
                "title": "세션",
                "role_key": "CTO",
                "current_model": "gpt-5.6-sol",
                "message_count": 3,
                "pinned": False,
                "tags": [],
                "completed_at": now,
                "created_at": now,
                "updated_at": now,
            }
            return [
                {**base, "session_id": working_session, "state": "working", "execution_id": uuid4()},
                {**base, "session_id": completed_session, "state": "completed_unread", "execution_id": uuid4()},
            ]

    monkeypatch.setattr(chat_service, "get_pool", lambda: FakePool(FakeConn()))
    result = asyncio.run(
        chat_service.list_session_attention(tenant_id=TENANT_ID, user_id=USER_ID)
    )

    assert result["working_count"] == 1
    assert result["completed_unread_count"] == 1
    assert [item["session_id"] for item in result["items"]] == [working_session, completed_session]


def test_acknowledge_session_attention_is_tenant_fenced(monkeypatch):
    session_id = uuid4()

    class FakeConn:
        executed = False

        async def fetchrow(self, query, *args):
            assert "s.tenant_id = $2" in query
            assert args == (session_id, UUID(TENANT_ID))
            return None

        async def execute(self, *_args):
            self.executed = True

    conn = FakeConn()
    monkeypatch.setattr(chat_service, "get_pool", lambda: FakePool(conn))
    result = asyncio.run(
        chat_service.acknowledge_session_attention(
            str(session_id), tenant_id=TENANT_ID, user_id=USER_ID
        )
    )

    assert result is None
    assert conn.executed is False


def test_acknowledge_session_attention_persists_latest_completion(monkeypatch):
    now = datetime.now(timezone.utc)
    session_id = uuid4()
    execution_id = uuid4()

    class FakeConn:
        execute_args = None

        async def fetchrow(self, query, *args):
            assert args == (session_id, UUID(TENANT_ID))
            return {
                "session_id": session_id,
                "execution_id": execution_id,
                "completed_at": now,
            }

        async def execute(self, query, *args):
            assert "ON CONFLICT (tenant_id, user_id, session_id)" in query
            self.execute_args = args

    conn = FakeConn()
    monkeypatch.setattr(chat_service, "get_pool", lambda: FakePool(conn))
    result = asyncio.run(
        chat_service.acknowledge_session_attention(
            str(session_id), tenant_id=TENANT_ID, user_id=USER_ID
        )
    )

    assert result is not None
    assert result["acknowledged"] is True
    assert result["execution_id"] == execution_id
    assert conn.execute_args[:5] == (
        UUID(TENANT_ID),
        USER_ID,
        session_id,
        execution_id,
        now,
    )


def test_attention_migration_is_additive_and_idempotent():
    sql = open("migrations/20260919_chat_session_attention.sql", encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS chat_session_attention_users" in sql
    assert "CREATE TABLE IF NOT EXISTS chat_session_attention_acknowledgements" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_chat_turn_executions_session_completed_attention" in sql
    assert "DROP TABLE" not in sql.upper()
