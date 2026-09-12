"""WP04 read-model, cursor, migration, and fenced-repair contract tests."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from app.models.chat import ChatMessageProjectionV2, ChatMessagesPageV2Out
from app.services import chat_protocol, chat_read_model, chat_repair, chat_service


def test_streaming_revision_and_checkpoint_writes_are_bounded():
    """T11/T44: token flushes use SSE and a one-second checkpoint cadence."""
    migration = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "174_chat_read_model_expand.sql"
    ).read_text(encoding="utf-8")

    assert "OLD.intent = 'streaming_placeholder'" in migration
    assert "NEW.intent = 'streaming_placeholder'" in migration
    assert "clock_timestamp() - INTERVAL '1 second'" in migration
    assert "terminal execution trigger performs an unconditional final flush" in migration


class _Context:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def test_signed_composite_cursor_is_ttl_and_scope_bound(monkeypatch):
    """T10/T35: signature, expiry, and every authorization/projection scope fail closed."""
    secret = "wp04-test-secret-" * 3
    tenant_id, session_id, message_id = uuid4(), uuid4(), uuid4()
    created_at = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    monkeypatch.setenv("AADS_CHAT_CURSOR_HMAC_SECRET", secret)

    cursor = chat_read_model.encode_message_cursor(
        created_at=created_at,
        message_id=message_id,
        tenant_id=tenant_id,
        user_id="user-17",
        session_id=session_id,
        projection="render",
        include_streaming=True,
        direction="before",
        now=1_000,
        ttl_seconds=90,
    )

    assert chat_read_model.decode_message_cursor(
        cursor,
        tenant_id=tenant_id,
        user_id="user-17",
        session_id=session_id,
        projection="render",
        include_streaming=True,
        direction="before",
        now=1_089,
    ) == (created_at, message_id)

    encoded_payload, signature = cursor.split(".", 1)
    tampered_cursor = (
        ("A" if encoded_payload[0] != "A" else "B")
        + encoded_payload[1:]
        + "."
        + signature
    )
    with pytest.raises(chat_read_model.ChatReadModelError) as tampered:
        chat_read_model.decode_message_cursor(
            tampered_cursor,
            tenant_id=tenant_id,
            user_id="user-17",
            session_id=session_id,
            projection="render",
            include_streaming=True,
            direction="before",
            now=1_001,
        )
    assert tampered.value.code == "invalid_chat_message_cursor"

    with pytest.raises(chat_read_model.ChatReadModelError) as wrong_scope:
        chat_read_model.decode_message_cursor(
            cursor,
            tenant_id=tenant_id,
            user_id="other-user",
            session_id=session_id,
            projection="render",
            include_streaming=True,
            direction="before",
            now=1_001,
        )
    assert wrong_scope.value.code == "chat_message_cursor_scope_mismatch"

    with pytest.raises(chat_read_model.ChatReadModelError) as expired:
        chat_read_model.decode_message_cursor(
            cursor,
            tenant_id=tenant_id,
            user_id="user-17",
            session_id=session_id,
            projection="render",
            include_streaming=True,
            direction="before",
            now=1_090,
        )
    assert expired.value.code == "expired_chat_message_cursor"


@pytest.mark.asyncio
async def test_v2_page_uses_raw_composite_boundary_and_read_only_transaction(monkeypatch):
    """T10/T35: same-timestamp rows use the raw `(created_at,id)` boundary before dedupe."""
    tenant_id, session_id = uuid4(), uuid4()
    created_at = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    ids = sorted((uuid4(), uuid4(), uuid4()))
    schema_ready = {
        "revisions": True,
        "outbox": True,
        "checkpoints": True,
        "content_version": True,
    }
    session = {
        "id": session_id,
        "message_revision": "9",
        "session_revision": "12",
        "is_active": False,
    }
    rows = [
        {
            "id": message_id,
            "session_id": session_id,
            "role": "user",
            "content": f"message-{index}",
            "model_used": None,
            "intent": None,
            "created_at": created_at,
            "edited_at": None,
            "content_version": 1,
            "generation_id": None,
            "segment_id": message_id,
        }
        for index, message_id in enumerate(ids)
    ]
    connection = AsyncMock()
    connection.fetchrow = AsyncMock(
        side_effect=[schema_ready, session]
    )
    connection.fetch = AsyncMock(return_value=rows)
    transaction_options = []

    def transaction(**options):
        transaction_options.append(options)
        return _Context()

    connection.transaction = transaction
    monkeypatch.setenv("AADS_CHAT_CURSOR_HMAC_SECRET", "x" * 32)
    monkeypatch.setattr(chat_read_model, "_pool", lambda: _Pool(connection))

    result = await chat_read_model.list_messages_v2(
        session_id=session_id,
        tenant_id=tenant_id,
        user_id="subject-42",
        limit=2,
        include_streaming=False,
        projection="render",
    )

    ChatMessagesPageV2Out.model_validate(result)
    assert [UUID(str(message["id"])) for message in result["messages"]] == ids[1:]
    boundary = chat_read_model.decode_message_cursor(
        result["page"]["next_cursor"],
        tenant_id=tenant_id,
        user_id="subject-42",
        session_id=session_id,
        projection="render",
        include_streaming=False,
        direction="before",
    )
    assert boundary == (created_at, ids[1])
    opposite_boundary = chat_read_model.decode_message_cursor(
        result["page"]["previous_cursor"],
        tenant_id=tenant_id,
        user_id="subject-42",
        session_id=session_id,
        projection="render",
        include_streaming=False,
        direction="after",
    )
    assert opposite_boundary == (created_at, ids[2])
    assert "ORDER BY created_at DESC, id DESC" in connection.fetch.await_args.args[0]

    connection.fetchrow.side_effect = [schema_ready, session]
    connection.fetch.return_value = rows[:1]
    older = await chat_read_model.list_messages_v2(
        session_id=session_id,
        tenant_id=tenant_id,
        user_id="subject-42",
        limit=2,
        cursor=result["page"]["next_cursor"],
        direction="before",
        include_streaming=False,
        projection="render",
    )
    older_ids = [UUID(str(message["id"])) for message in older["messages"]]
    newer_ids = [UUID(str(message["id"])) for message in result["messages"]]
    assert older_ids == ids[:1]
    assert set(older_ids).isdisjoint(newer_ids)
    assert sorted(older_ids + newer_ids) == ids
    assert "(created_at, id) < ($3::timestamptz, $4::uuid)" in (
        connection.fetch.await_args.args[0]
    )

    connection.fetchrow.side_effect = [schema_ready, session]
    connection.fetch.return_value = rows
    forward = await chat_read_model.list_messages_v2(
        session_id=session_id,
        tenant_id=tenant_id,
        user_id="subject-42",
        limit=2,
        direction="after",
        include_streaming=False,
        projection="render",
    )
    assert [UUID(str(message["id"])) for message in forward["messages"]] == ids[:2]
    assert forward["page"]["direction"] == "after"
    assert "ORDER BY created_at ASC, id ASC LIMIT $3" in connection.fetch.await_args.args[0]
    assert transaction_options == [
        {"isolation": "repeatable_read", "readonly": True},
        {"isolation": "repeatable_read", "readonly": True},
        {"isolation": "repeatable_read", "readonly": True},
    ]
    connection.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_v2_message_detail_exposes_content_version_without_backfill(monkeypatch):
    """T36/T43: detail hydration is additive and leaves the stored source untouched."""
    tenant_id, session_id, message_id = uuid4(), uuid4(), uuid4()
    connection = AsyncMock()
    connection.fetchrow = AsyncMock(
        side_effect=[
            {
                "revisions": True,
                "outbox": True,
                "checkpoints": True,
                "content_version": True,
            },
            {
                "id": message_id,
                "session_id": session_id,
                "role": "user",
                "content": "stored source",
                "model_used": None,
                "intent": None,
                "created_at": datetime.now(UTC),
                "edited_at": None,
                "content_version": 7,
                "generation_id": None,
                "segment_id": message_id,
                "wp04_session_active": False,
            },
        ]
    )
    connection.transaction = lambda **_options: _Context()
    monkeypatch.setenv("AADS_CHAT_CURSOR_HMAC_SECRET", "x" * 32)
    monkeypatch.setattr(chat_read_model, "_pool", lambda: _Pool(connection))

    result = await chat_read_model.get_message_v2(
        message_id=message_id,
        tenant_id=tenant_id,
        user_id="subject-42",
        projection="minimal",
    )

    assert result["schema_version"] == 2
    assert result["contract_version"] == 2
    assert result["content_version"] == "7"
    assert result["content_completeness"] == "full"
    assert result["segment_id"] == str(message_id)
    ChatMessageProjectionV2.model_validate(result)
    connection.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_projection_repairs_payload_without_get_side_effects():
    """T35: completed ledger state is visible while a GET-compatible projection performs no DML."""
    execution_id, message_id = uuid4(), uuid4()
    connection = AsyncMock()
    connection.fetch = AsyncMock(
        return_value=[
            {
                "id": message_id,
                "execution_id": execution_id,
                "assistant_message_id": message_id,
                "final_model": "claude-sonnet",
                "content_len": 12,
                "created_at": datetime.now(UTC) - timedelta(seconds=3),
            }
        ]
    )
    messages = [
        {
            "id": message_id,
            "execution_id": execution_id,
            "role": "assistant",
            "content": "완료된 답변\n\n⏳ _생성 중..._",
            "intent": "streaming_placeholder",
            "model_used": "streaming",
        }
    ]

    projected = await chat_service._repair_completed_execution_message_flags(
        connection,
        messages,
        "wp04_test",
        persist=False,
    )

    assert projected[0]["intent"] is None
    assert projected[0]["model_used"] == "claude-sonnet"
    assert "생성 중" not in projected[0]["content"]
    connection.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_repair_writer_claims_and_releases_exact_owner_epoch():
    """T19/T35: repair is explicit, active-slot-only, and guarded by an owner epoch."""
    tenant_id, session_id, execution_id = uuid4(), uuid4(), uuid4()
    connection = AsyncMock()
    connection.fetchrow = AsyncMock(return_value={"owner_epoch": 8})
    connection.fetch = AsyncMock(return_value=[])
    connection.fetchval = AsyncMock(return_value=8)
    connection.transaction = lambda: _Context()

    with (
        patch.object(chat_repair.svc, "_is_local_active_api_slot", return_value=True),
        patch.object(chat_repair, "get_pool", return_value=_Pool(connection)),
        patch.object(
            chat_repair.svc,
            "_repair_completed_execution_message_flags",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = await chat_repair.repair_completed_execution_projection(
            session_id=session_id,
            execution_id=execution_id,
            tenant_id=tenant_id,
            reason="operator_requested",
        )

    claim_sql = connection.fetchrow.await_args.args[0]
    release_sql = connection.fetchval.await_args.args[0]
    assert "owner_epoch = te.owner_epoch + 1" in claim_sql
    assert "te.status = 'completed'" in claim_sql
    assert "owner_instance = $3" in release_sql
    assert "owner_epoch = $4" in release_sql
    assert result["claimed_owner_epoch"] == "8"


def test_migration_split_capabilities_and_symbol_preservation(monkeypatch):
    """T11/T36/T43: schema is additive, indexes are online, and activation stays gated."""
    expand = Path("migrations/174_chat_read_model_expand.sql").read_text(encoding="utf-8")
    indexes = Path(
        "migrations/175_chat_read_model_indexes_concurrently.sql"
    ).read_text(encoding="utf-8")
    read_model_tree = ast.parse(inspect.getsource(chat_read_model))
    dml_calls = {
        node.func.attr
        for node in ast.walk(read_model_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "executemany", "copy_records_to_table"}
    }

    assert "BEGIN;" in expand and "COMMIT;" in expand
    assert "CREATE INDEX CONCURRENTLY" not in expand
    assert "CREATE INDEX CONCURRENTLY" in indexes
    assert "BEGIN;" not in indexes and "COMMIT;" not in indexes
    assert "PRIMARY KEY (tenant_id, session_id)" in expand
    assert "ON CONFLICT (tenant_id, session_id) DO UPDATE" in expand
    assert "chat_session_revisions" in expand
    assert "chat_outbox" in expand
    assert "chat_execution_checkpoints" in expand
    assert "CREATE TRIGGER trg_chat_session_revision" in expand
    assert "CREATE TRIGGER trg_chat_message_revision" in expand
    assert "CREATE TRIGGER trg_chat_artifact_revision" in expand
    assert "CREATE TRIGGER trg_chat_execution_revision" in expand
    assert dml_calls == set()
    assert hasattr(chat_service, "_resume_interrupted_streams_legacy_disabled")
    assert hasattr(chat_service, "_promote_inactive_streaming_placeholders")
    assert hasattr(chat_protocol, "_version_from_datetime")

    monkeypatch.setenv("AADS_CHAT_V2_READ_MODEL_ENABLED", "true")
    monkeypatch.setenv("AADS_CHAT_WP04_MIGRATION_READY", "true")
    monkeypatch.setenv("AADS_CHAT_WP04_CROSS_VERSION_READY", "false")
    monkeypatch.setenv("AADS_CHAT_CURSOR_HMAC_SECRET", "x" * 32)
    capabilities = chat_protocol.chat_protocol_capabilities()
    assert chat_protocol.negotiate_contract_version(None) == 1
    assert capabilities["production_ready"] is False
    assert capabilities["read_model"]["activation_gates_passed"] is False
    assert capabilities["read_model"]["get_is_read_only"] is True

    monkeypatch.setenv("AADS_CHAT_WP04_CROSS_VERSION_READY", "true")
    ready_capabilities = chat_protocol.chat_protocol_capabilities()
    assert ready_capabilities["production_ready"] is False
    assert ready_capabilities["read_model"]["activation_gates_passed"] is True
    assert ready_capabilities["read_model"]["production_ready"] is True
