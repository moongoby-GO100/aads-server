"""Run the actual lease SQL against connection-local temporary tables only.

Opt in with AADS_BUBBLE_DB_TEST=1 and DATABASE_URL. No public data is modified.
"""
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest

from app.services import chat_service as svc


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("AADS_BUBBLE_DB_TEST") != "1", reason="explicit database test opt-in required")
@pytest.mark.parametrize("status,error,newer_intent,newer_deleted,allow", [
    ("interrupted", "llm_first_response_timeout_after_198s", None, False, True),
    ("interrupted", "stale_superseded_by_newer_user_message", None, False, False),
    ("interrupted", "superseded while preserving partial response", None, False, False),
    ("completed", "", None, False, False),
    ("cancelled", "", None, False, False),
    ("interrupted", "timeout", "normal", False, False),
    ("interrupted", "timeout", "queued_interrupt", False, True),
    ("interrupted", "timeout", "system_trigger", False, True),
    ("interrupted", "timeout", "normal", True, True),
])
async def test_claim_cannot_resurrect_superseded_turn_with_manual_override(status, error, newer_intent, newer_deleted, allow):
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], host=os.getenv("AADS_BUBBLE_DB_HOST"))
    try:
        # pg_temp shadows public tables for this one connection; explicit
        # search_path prevents any accidental use of production tables.
        await conn.execute("SET search_path TO pg_temp")
        await conn.execute("""
            CREATE TEMP TABLE chat_messages (
                id uuid, session_id uuid, role text, intent text, content text,
                created_at timestamptz, deleted_at timestamptz
            );
            CREATE TEMP TABLE chat_turn_executions (
                id uuid, session_id uuid, user_message_id uuid, status text,
                owner_instance text, owner_epoch bigint, error_message text,
                started_at timestamptz, attempt_started_at timestamptz,
                heartbeat_at timestamptz, lease_expires_at timestamptz,
                completed_at timestamptz, updated_at timestamptz
            );
        """)
        now = datetime.now(timezone.utc)
        sid, eid, uid = uuid4(), uuid4(), uuid4()
        await conn.execute("INSERT INTO chat_messages VALUES ($1,$2,'user','normal','question',$3,NULL)", uid, sid, now)
        if newer_intent:
            await conn.execute("INSERT INTO chat_messages VALUES ($1,$2,'user',$3,'followup',$4,$5)",
                               uuid4(), sid, newer_intent, now + timedelta(seconds=1), now if newer_deleted else None)
        await conn.execute("""INSERT INTO chat_turn_executions
            (id,session_id,user_message_id,status,owner_epoch,error_message,started_at)
            VALUES ($1,$2,$3,$4,535,$5,$6)""", eid, sid, uid, status, error, now)
        epoch = await svc._claim_execution_lease(
            conn, eid, status="retrying", error_message="manual_resume_claimed", allow_any_epoch=True,
        )
        assert epoch == (536 if allow else None)
        stored_epoch = await conn.fetchval("SELECT owner_epoch FROM chat_turn_executions WHERE id=$1", eid)
        assert stored_epoch == (536 if allow else 535)
        if allow:
            # A repeated request cannot take an unexpired lease even when
            # handled by the same instance and supplied a budget override.
            assert await svc._claim_execution_lease(conn, eid, status="retrying", allow_any_epoch=True) is None
    finally:
        await conn.close()
