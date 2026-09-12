"""WP03 server unit/contract/fault coverage for T05-T09/T21/T36.

All fixtures are synthetic.  No DB, Redis, provider, or network connection is
made by this module.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.models.chat import ChatEventEnvelopeV2, ChatStreamSnapshotOut
from app.services import chat_protocol, redis_stream, stream_worker

SESSION_ID = "00000000-0000-4000-8000-000000000101"
EXECUTION_ID = "00000000-0000-4000-8000-000000000102"
MESSAGE_ID = "00000000-0000-4000-8000-000000000103"
TENANT_ID = "00000000-0000-4000-8000-000000000104"


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


async def _collect(generator):
    return [item async for item in generator]


def _payload(sse: str) -> dict:
    data_line = next(line for line in sse.splitlines() if line.startswith("data:"))
    return json.loads(data_line.removeprefix("data:"))


def test_capability_and_cursor_negotiation_preserve_v1_default():
    """T07/T36: v2 is discoverable while unversioned and legacy cursors stay valid."""
    capabilities = chat_protocol.chat_protocol_capabilities()

    assert chat_protocol.negotiate_contract_version(None) == 1
    assert chat_protocol.negotiate_contract_version("2") == 2
    assert capabilities["supported_contract_versions"] == [1, 2]
    assert capabilities["resume_cursor"]["parameter"] == "last_applied_event_id"
    assert capabilities["legacy_compatibility"]["unwrapped_sse_events"] is True
    assert (
        chat_protocol.resolve_resume_cursor(
            contract_version=1,
            last_applied_event_id=None,
            legacy_last_event_id="10-2",
            header_last_event_id=None,
        )
        == "10-2"
    )
    assert (
        chat_protocol.resolve_resume_cursor(
            contract_version=2,
            last_applied_event_id="10-1",
            legacy_last_event_id=None,
            header_last_event_id=None,
        )
        == "10-1"
    )

    with pytest.raises(chat_protocol.ChatProtocolError) as conflict:
        chat_protocol.resolve_resume_cursor(
            contract_version=2,
            last_applied_event_id="10-1",
            legacy_last_event_id="10-2",
            header_last_event_id=None,
        )
    assert conflict.value.code == "conflicting_chat_event_cursors"


@pytest.mark.asyncio
async def test_common_v2_adapter_handles_chunking_crlf_multidata_and_unknown_event():
    """T06/T36: all live paths can share one frame parser and additive event adapter."""

    async def source():
        yield 'retry: 3000\r\n\r\ndata: {"type":"stream_start",'
        yield f'\r\ndata: "execution_id":"{EXECUTION_ID}"}}\r\n\r\n'
        yield 'data:{"type":"future_additive","value":7}\n\n'

    events = await _collect(chat_protocol.adapt_sse_stream(source(), session_id=SESSION_ID))

    assert events[0] == "retry:3000\n\n"
    started = _payload(events[1])
    additive = _payload(events[2])
    assert started["schema_version"] == 2
    assert started["type"] == "execution.phase"
    assert started["execution_id"] == EXECUTION_ID
    assert started["generation_id"] == EXECUTION_ID
    assert additive["type"] == "legacy.future_additive"
    assert additive["payload"] == {"value": 7, "legacy_type": "future_additive"}
    ChatEventEnvelopeV2.model_validate(started)
    ChatEventEnvelopeV2.model_validate(additive)


@pytest.mark.asyncio
async def test_invalid_event_requests_snapshot_without_advancing_event_id():
    """T06/T07: a critical invalid frame cannot become an applied reconnect cursor."""

    async def source():
        yield "id:10-7\ndata:{not-json}\n\n"
        yield 'id:10-8\ndata:{"type":"delta","content":"must not pass"}\n\n'

    events = await _collect(
        chat_protocol.adapt_sse_stream(
            source(),
            session_id=SESSION_ID,
            execution_id=EXECUTION_ID,
        )
    )

    assert len(events) == 1
    assert not events[0].startswith("id:")
    recovery = _payload(events[0])
    assert recovery["type"] == "stream.snapshot_required"
    assert recovery["event_id"] is None
    assert recovery["payload"]["failed_event_id"] == "10-7"


@pytest.mark.asyncio
async def test_transport_eof_does_not_synthesize_execution_completion():
    """T05: an ordinary reader EOF is not an execution terminal transition."""

    async def source():
        yield 'id:10-1\ndata:{"type":"delta","content":"partial"}\n\n'

    events = await _collect(
        chat_protocol.adapt_sse_stream(
            source(),
            session_id=SESSION_ID,
            execution_id=EXECUTION_ID,
        )
    )

    assert len(events) == 1
    event = _payload(events[0])
    assert event["type"] == "message.delta"
    assert event["payload"]["legacy_type"] == "delta"


@pytest.mark.asyncio
async def test_legacy_replay_bytes_and_completion_distinction_are_unchanged():
    """T05/T36: the default path remains the exact v1 replay fixture."""
    cached = [
        {
            "id": "10-1",
            "data": 'data: {"type":"delta","content":"한글"}\n\n',
        },
        {"id": "10-2", "done": True},
    ]
    with patch.object(
        stream_worker._rs,
        "read_tokens_after",
        new=AsyncMock(return_value=cached),
    ):
        events = await _collect(stream_worker.deliver_sse(EXECUTION_ID, "0", timeout_sec=1))

    assert events == [
        'id:10-1\ndata: {"type":"delta","content":"한글"}\n\n',
        f"data: {json.dumps({'type': 'resume_done'})}\n\n",
    ]


@pytest.mark.asyncio
async def test_v2_replay_envelopes_event_ids_but_does_not_mark_execution_terminal():
    """T05-T08/T21: replay completion is transport state, not a terminal DB write."""
    cached = [
        {
            "id": "10-1",
            "idx": 4,
            "ts": "1789214400.0",
            "data": 'data: {"type":"delta","content":"한글 👩🏽‍💻"}\n\n',
        },
        {"id": "10-2", "idx": 5, "ts": "1789214401.0", "done": True},
    ]
    stream_info = {
        "exists": True,
        "first_event_id": "10-0",
        "first_event_index": 0,
        "last_event_id": "10-2",
        "retention_trimmed": False,
        "is_done": True,
    }
    with (
        patch.object(
            stream_worker._rs,
            "get_stream_info",
            new=AsyncMock(return_value=stream_info),
        ),
        patch.object(
            stream_worker._rs,
            "read_tokens_after",
            new=AsyncMock(return_value=cached),
        ),
    ):
        events = await _collect(
            stream_worker.deliver_sse(
                EXECUTION_ID,
                "10-0",
                timeout_sec=1,
                contract_version=2,
                session_id=SESSION_ID,
                execution_id=EXECUTION_ID,
                owner_epoch=12,
            )
        )

    delta = _payload(events[0])
    replay_done = _payload(events[1])
    assert events[0].startswith("id:10-1\n")
    assert delta["type"] == "message.delta"
    assert delta["sequence"] == "4"
    assert delta["owner_epoch"] == "12"
    assert replay_done["type"] == "stream.replay_done"
    assert replay_done["event_id"] == "10-2"
    assert replay_done["payload"]["execution_terminal"] is False


@pytest.mark.asyncio
async def test_trimmed_applied_cursor_fails_to_snapshot_before_replay():
    """T07 fault: Redis trim cannot silently skip tokens."""
    read_tokens = AsyncMock(return_value=[])
    with (
        patch.object(
            stream_worker._rs,
            "get_stream_info",
            new=AsyncMock(
                return_value={
                    "exists": True,
                    "first_event_id": "20-4",
                    "last_event_id": "20-9",
                    "is_done": False,
                }
            ),
        ),
        patch.object(stream_worker._rs, "read_tokens_after", new=read_tokens),
    ):
        events = await _collect(
            stream_worker.deliver_sse(
                EXECUTION_ID,
                "10-7",
                timeout_sec=1,
                contract_version=2,
                session_id=SESSION_ID,
                execution_id=EXECUTION_ID,
            )
        )

    assert len(events) == 1
    assert read_tokens.await_count == 0
    recovery = _payload(events[0])
    assert recovery["type"] == "stream.snapshot_required"
    assert recovery["payload"]["reason"] == "cursor_before_retention"
    assert recovery["payload"]["server_high_watermark"] == "20-9"


@pytest.mark.asyncio
async def test_v2_replay_requires_snapshot_before_initial_cursor_zero():
    """T07: an initial replay cannot assume Redis still covers the whole response."""
    with patch.object(
        stream_worker._rs,
        "get_stream_info",
        new=AsyncMock(
            return_value={
                "exists": True,
                "first_event_id": "10-1",
                "first_event_index": 0,
                "last_event_id": "10-5",
                "retention_trimmed": True,
                "is_done": False,
            }
        ),
    ):
        events = await _collect(
            stream_worker.deliver_sse(
                EXECUTION_ID,
                "0",
                timeout_sec=1,
                contract_version=2,
                session_id=SESSION_ID,
                execution_id=EXECUTION_ID,
            )
        )

    recovery = _payload(events[0])
    assert recovery["type"] == "stream.snapshot_required"
    assert recovery["payload"]["reason"] == "snapshot_required_before_replay"


@pytest.mark.asyncio
async def test_redis_info_advertises_retention_bounds_without_changing_done_state():
    """T05/T07: first/high-watermark metadata is additive to legacy stream state."""
    redis = AsyncMock()
    redis.exists = AsyncMock(return_value=True)
    redis.xlen = AsyncMock(return_value=3)
    redis.xinfo_stream = AsyncMock(return_value={"entries-added": 3, "max-deleted-entry-id": "0-0"})
    redis.xrange = AsyncMock(return_value=[("10-1", {"idx": "0", "data": "first"})])
    redis.xrevrange = AsyncMock(return_value=[("10-3", {"done": "true", "data": ""})])
    with patch.object(redis_stream, "_get_redis", new=AsyncMock(return_value=redis)):
        info = await redis_stream.get_stream_info(EXECUTION_ID)

    assert info == {
        "exists": True,
        "length": 3,
        "is_done": True,
        "first_event_id": "10-1",
        "first_event_index": 0,
        "last_event_id": "10-3",
        "retention_trimmed": False,
        "stream_key": f"chat:stream:{EXECUTION_ID}",
    }


@pytest.mark.asyncio
async def test_redis_event_captures_owner_epoch_at_publish_time():
    """T21: replay carries the producing fence epoch, never a later lease owner."""
    redis = AsyncMock()
    redis.xadd = AsyncMock(return_value="10-1")
    redis.expire = AsyncMock()
    with patch.object(redis_stream, "_get_redis", new=AsyncMock(return_value=redis)):
        event_id = await redis_stream.publish_token(
            EXECUTION_ID,
            'data: {"type":"delta","content":"safe"}\n\n',
            0,
            owner_epoch=12,
        )

    assert event_id == "10-1"
    fields = redis.xadd.await_args.args[1]
    assert fields["idx"] == "0"
    assert fields["owner_epoch"] == "12"


@pytest.mark.asyncio
async def test_snapshot_pairs_db_coverage_with_separate_redis_high_watermark():
    """T07-T09/T21: snapshot reads are fenced-state neutral and cursors stay distinct."""
    connection = AsyncMock()
    connection.fetchrow = AsyncMock(
        return_value={
            "session_id": UUID(SESSION_ID),
            "message_count": 9,
            "session_updated_at": datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
            "execution_id": UUID(EXECUTION_ID),
            "execution_phase": "running",
            "owner_epoch": 12,
            "covers_through_event_id": "10-2",
            "message_id": UUID(MESSAGE_ID),
            "content": "동일 bubble의 DB checkpoint",
            "intent": "streaming_placeholder",
            "tools_called": '[{"type":"tool_use","tool_use_id":"tool-1"}]',
            "message_created_at": datetime(2026, 9, 12, 11, 59, tzinfo=UTC),
            "message_edited_at": datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
        }
    )
    with (
        patch("app.core.db_pool.get_pool", return_value=_Pool(connection)),
        patch.object(
            chat_protocol.redis_stream,
            "get_stream_info",
            new=AsyncMock(
                return_value={
                    "exists": True,
                    "first_event_id": "10-1",
                    "last_event_id": "10-5",
                    "retention_trimmed": False,
                    "is_done": False,
                }
            ),
        ),
    ):
        snapshot = await chat_protocol.get_stream_snapshot(
            session_id=UUID(SESSION_ID),
            tenant_id=UUID(TENANT_ID),
            execution_id=UUID(EXECUTION_ID),
            last_applied_event_id="10-1",
        )

    validated = ChatStreamSnapshotOut.model_validate(snapshot)
    assert validated.message_id == UUID(MESSAGE_ID)
    assert validated.segment_id == UUID(MESSAGE_ID)
    assert validated.generation_id == UUID(EXECUTION_ID)
    assert validated.owner_epoch == "12"
    assert validated.covers_through_event_id == "10-2"
    assert validated.server_high_watermark == "10-5"
    assert validated.retention_trimmed is False
    assert validated.last_applied_event_id == "10-1"
    assert validated.resume_from_event_id == "10-2"
    assert validated.snapshot_required is True
    assert validated.replay_required is True
    sql = connection.fetchrow.await_args.args[0]
    assert "SELECT" in sql
    assert "UPDATE" not in sql
    assert "DELETE" not in sql
    assert "owner_instance" not in sql


def test_envelope_schema_rejects_critical_version_mismatch():
    """T36: critical schema drift is visible instead of silently coerced."""
    envelope = chat_protocol.build_event_envelope(
        {"type": "delta", "content": "ok"},
        event_id="10-1",
        session_id=SESSION_ID,
        execution_id=EXECUTION_ID,
    )
    ChatEventEnvelopeV2.model_validate(envelope)

    with pytest.raises(ValidationError):
        ChatEventEnvelopeV2.model_validate({**envelope, "schema_version": 3})
