"""Versioned chat streaming contracts and legacy SSE adapters.

The production chat stream predates an explicit event schema.  Contract v1 is
therefore intentionally left byte-for-byte compatible.  Callers that negotiate
contract v2 receive a typed envelope and must reconnect from the last event that
their reducer actually applied, never from a server-advertised high watermark.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.services import redis_stream

CHAT_CONTRACT_V1 = 1
CHAT_CONTRACT_V2 = 2
DEFAULT_CHAT_CONTRACT_VERSION = CHAT_CONTRACT_V1
SUPPORTED_CHAT_CONTRACT_VERSIONS = (CHAT_CONTRACT_V1, CHAT_CONTRACT_V2)
CHAT_EVENT_SCHEMA_VERSION = 2

_REDIS_EVENT_ID_RE = re.compile(r"^(?:0|\d+-\d+)$")
_LEGACY_EVENT_TYPES: dict[str, str] = {
    "delta": "message.delta",
    "done": "message.final",
    "message_stop": "message.final",
    "partial_preserved": "message.snapshot",
    "stream_start": "execution.phase",
    "stream_reset": "stream.reset",
    "heartbeat": "stream.heartbeat",
    "resume_done": "stream.replay_done",
    "resume_generating": "stream.replay_pending",
    "resume_unavailable": "stream.resume_unavailable",
    "resume_timeout": "stream.resume_timeout",
    "tool_use": "tool.started",
    "tool_result": "tool.result",
    "thinking": "message.thinking",
    "sources": "message.sources",
    "model_info": "execution.model",
    "model_fallback": "execution.model_changed",
    "retry_progress": "execution.retry_progress",
    "progress": "execution.progress",
    "task_plan": "execution.plan",
    "research_start": "research.started",
    "research_progress": "research.progress",
    "research_complete": "research.completed",
    "interrupt_applied": "command.applied",
    "error": "stream.error",
}


class ChatProtocolError(ValueError):
    """A client-visible chat protocol negotiation or cursor error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def negotiate_contract_version(
    requested: str | int | None,
    header_value: str | None = None,
) -> int:
    """Resolve an explicit version while keeping unversioned clients on v1."""
    raw = requested if requested not in (None, "") else header_value
    if raw in (None, ""):
        return DEFAULT_CHAT_CONTRACT_VERSION
    try:
        version = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ChatProtocolError(
            "invalid_chat_contract_version",
            f"chat contract version must be one of {SUPPORTED_CHAT_CONTRACT_VERSIONS}",
        ) from exc
    if version not in SUPPORTED_CHAT_CONTRACT_VERSIONS:
        raise ChatProtocolError(
            "unsupported_chat_contract_version",
            f"chat contract version {version} is not supported",
        )
    return version


def validate_event_cursor(cursor: str | None) -> str | None:
    """Validate the currently Redis-backed opaque replay cursor."""
    if cursor in (None, ""):
        return None
    value = str(cursor).strip()
    if not _REDIS_EVENT_ID_RE.fullmatch(value):
        raise ChatProtocolError(
            "invalid_chat_event_cursor",
            "chat event cursor must be a Redis stream id or the initial cursor 0",
        )
    return value


def resolve_resume_cursor(
    *,
    contract_version: int,
    last_applied_event_id: str | None,
    legacy_last_event_id: str | None,
    header_last_event_id: str | None,
) -> str:
    """Select a reconnect cursor without conflating it with a high watermark.

    V2 names the client-owned cursor explicitly.  During the compatibility
    window the old query/header name is accepted as an alias, but conflicting
    values fail closed instead of skipping events.
    """
    applied = validate_event_cursor(last_applied_event_id)
    legacy_query = validate_event_cursor(legacy_last_event_id)
    legacy_header = validate_event_cursor(header_last_event_id)
    supplied = {value for value in (applied, legacy_query, legacy_header) if value is not None}
    if contract_version == CHAT_CONTRACT_V2 and len(supplied) > 1:
        raise ChatProtocolError(
            "conflicting_chat_event_cursors",
            "last_applied_event_id, last_event_id and Last-Event-ID must agree",
        )
    if contract_version == CHAT_CONTRACT_V2:
        return applied or legacy_query or legacy_header or "0"
    return legacy_query or legacy_header or applied or "0"


def compare_redis_event_ids(left: str, right: str) -> int:
    """Compare Redis stream ids numerically, never lexically."""
    left_value = validate_event_cursor(left)
    right_value = validate_event_cursor(right)
    if left_value is None or right_value is None:
        raise ChatProtocolError("invalid_chat_event_cursor", "event cursor is required")

    def _parts(value: str) -> tuple[int, int]:
        if value == "0":
            return (0, 0)
        milliseconds, sequence = value.split("-", 1)
        return int(milliseconds), int(sequence)

    left_parts = _parts(left_value)
    right_parts = _parts(right_value)
    return (left_parts > right_parts) - (left_parts < right_parts)


CHAT_PROTOCOL_V2_CAPABILITY = "chat.protocol.v2"
_PROTOCOL_V2_FLAG_ENV = "AADS_CHAT_PROTOCOL_V2_ENABLED"
_PROTOCOL_V2_BROWSER_CONTRACT_ENV = "AADS_CHAT_V2_BROWSER_CONTRACT_VERIFIED"
_BASE_CHAT_CAPABILITIES = (
    "chat.event_envelope.v2",
    "chat.snapshot_coverage.v2",
    "chat.server_high_watermark.v2",
    "chat.applied_cursor.v2",
    "chat.transport_execution_separation.v2",
    "chat.read_model.v2",
    "chat.composite_cursor.v2",
    "chat.revision_outbox.v2",
    "chat.explicit_fenced_repair.v2",
    "chat.command_lifecycle.v2",
    "chat.command_idempotency.v2",
    "chat.generation_identity.v2",
    "chat.legacy_sse.v1",
)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def chat_protocol_v2_activation_gates() -> dict[str, bool]:
    """Report each operational gate that must pass before v2 may go live.

    The browser only switches adapters when the server advertises
    ``chat.protocol.v2``; every gate is operator-controlled, so an unset
    environment keeps the legacy v1 contract byte-for-byte unchanged.
    """
    from app.services.chat_commands import wp05_migration_ready
    from app.services.chat_read_model import (
        chat_cursor_secret_configured,
        wp04_read_model_activation_ready,
    )

    return {
        "chat.protocol_v2 feature flag": _env_flag(_PROTOCOL_V2_FLAG_ENV),
        "wp04_read_model_migration": wp04_read_model_activation_ready(),
        "dedicated_cursor_hmac_secret": chat_cursor_secret_configured(),
        "wp04_cross_version_contract_tests": _env_flag(
            "AADS_CHAT_WP04_CROSS_VERSION_READY"
        ),
        "atomic_snapshot_checkpoint": _env_flag("AADS_CHAT_WP04_MIGRATION_READY"),
        "stable_generation_identity": wp05_migration_ready(),
        "cross_version_browser_contract_tests": _env_flag(
            _PROTOCOL_V2_BROWSER_CONTRACT_ENV
        ),
    }


def chat_protocol_v2_activation_ready() -> bool:
    """True only when every activation gate passes."""
    return all(chat_protocol_v2_activation_gates().values())


def advertised_chat_capabilities() -> list[str]:
    """Capability strings a client may act on, computed from live gates."""
    capabilities = list(_BASE_CHAT_CAPABILITIES)
    if chat_protocol_v2_activation_ready():
        capabilities.append(CHAT_PROTOCOL_V2_CAPABILITY)
    return capabilities


def chat_protocol_capabilities() -> dict[str, Any]:
    """Return the stable, additive capability advertisement."""
    from app.services.chat_read_model import (
        chat_cursor_ttl_seconds,
        wp04_read_model_activation_ready,
    )

    gates = chat_protocol_v2_activation_gates()
    protocol_v2_ready = all(gates.values())
    migration_ready = gates["atomic_snapshot_checkpoint"]
    cross_version_ready = gates["wp04_cross_version_contract_tests"]
    cursor_secret_ready = gates["dedicated_cursor_hmac_secret"]
    command_lifecycle_ready = gates["stable_generation_identity"]
    return {
        "contract_version": CHAT_CONTRACT_V2,
        "default_contract_version": DEFAULT_CHAT_CONTRACT_VERSION,
        "supported_contract_versions": list(SUPPORTED_CHAT_CONTRACT_VERSIONS),
        "event_schema_version": CHAT_EVENT_SCHEMA_VERSION,
        # Global protocol readiness also depends on later browser/generation
        # gates advertised below; WP04 exposes its narrower readiness separately.
        "production_ready": protocol_v2_ready,
        "activation_requires": list(gates.keys()),
        "activation_gates": dict(gates),
        "pending_activation_requires": [
            name for name, passed in gates.items() if not passed
        ],
        "capabilities": advertised_chat_capabilities(),
        "event_envelope": {
            "schema_version": CHAT_EVENT_SCHEMA_VERSION,
            "required_fields": [
                "schema_version",
                "type",
                "occurred_at",
                "payload",
            ],
            "nullable_scope_fields": [
                "event_id",
                "session_id",
                "execution_id",
                "owner_epoch",
                "generation_id",
                "segment_id",
                "sequence",
            ],
            "unknown_event_policy": "ignore_additive",
            "critical_invalid_policy": "snapshot_required",
            "sequence_contiguous": False,
            "sequence_semantics": "legacy_producer_index_advisory",
        },
        "resume_cursor": {
            "parameter": "last_applied_event_id",
            "legacy_parameter": "last_event_id",
            "header_alias": "Last-Event-ID",
            "ownership": "client_applied_only",
        },
        "snapshot": {
            "endpoint": "/api/v1/chat/sessions/{session_id}/stream-snapshot",
            "coverage_field": "covers_through_event_id",
            "high_watermark_field": "server_high_watermark",
            "retention_field": "retention_trimmed",
            "retention_boundary_field": "max_deleted_event_id",
            "coverage_atomic": migration_ready,
            # WP05 keys generation identity by (execution_id, owner_epoch), so a
            # reconnect to the same fence always resolves the same generation and
            # every new lease claim starts a new one.
            "generation_identity": (
                "stable_per_owner_epoch" if command_lifecycle_ready else "unavailable"
            ),
        },
        "read_model": {
            "view_endpoint": "/api/v1/chat/sessions/{session_id}/view",
            "changes_endpoint": "/api/v1/chat/sessions/{session_id}/changes",
            "migration_ready": migration_ready,
            "cursor_secret_configured": cursor_secret_ready,
            "cross_version_verified": cross_version_ready,
            "activation_gates_passed": wp04_read_model_activation_ready(),
            "production_ready": wp04_read_model_activation_ready(),
            "cursor_order": ["created_at", "id"],
            "cursor_scope": [
                "tenant_id",
                "user_id",
                "session_id",
                "projection",
                "include_streaming",
                "direction",
            ],
            "cursor_ttl_seconds": chat_cursor_ttl_seconds(),
            "get_is_read_only": True,
        },
        "command_lifecycle": {
            "submit_endpoint": "/api/v1/chat/sessions/{session_id}/commands",
            "state_endpoint": "/api/v1/chat/sessions/{session_id}/commands/{command_id}",
            "generation_endpoint": "/api/v1/chat/executions/{execution_id}/generation",
            "recovery_endpoint": "/api/v1/chat/commands/recover",
            "idempotency_header": "Idempotency-Key",
            "idempotency_scope": [
                "tenant_id",
                "session_id",
                "command_type",
                "idempotency_key",
            ],
            "key_reuse_policy": "fail_closed_on_fingerprint_mismatch",
            "durable_command_types": ["interrupt", "stop", "resume"],
            # send/retry answer over SSE; their durable identity is the
            # generation, and settling their command row is not wired yet.
            "streaming_command_types": ["send", "retry"],
            "statuses": ["accepted", "running", "succeeded", "failed", "superseded"],
            "terminal_statuses": ["succeeded", "failed", "superseded"],
            "fence_field": "owner_epoch",
            "generation_key": ["execution_id", "owner_epoch"],
            "migration_ready": command_lifecycle_ready,
            "production_ready": protocol_v2_ready,
        },
        "legacy_compatibility": {
            "default_contract_version": CHAT_CONTRACT_V1,
            "unwrapped_sse_events": True,
            "legacy_cursor_alias": True,
        },
    }


@dataclass(frozen=True)
class ParsedSSEFrame:
    data: str | None
    event_id: str | None
    event: str | None
    retry: int | None


class SSEFrameDecoder:
    """Incremental WHATWG-style frame decoder for the server's SSE adapters."""

    def __init__(self) -> None:
        self._line = ""
        self._saw_cr = False
        self._data_lines: list[str] = []
        self._event_id: str | None = None
        self._event: str | None = None
        self._retry: int | None = None
        self._has_fields = False

    def feed(self, chunk: str) -> list[ParsedSSEFrame]:
        frames: list[ParsedSSEFrame] = []
        for character in str(chunk):
            if self._saw_cr:
                self._saw_cr = False
                if character == "\n":
                    continue
            if character == "\r":
                frames.extend(self._consume_line())
                self._saw_cr = True
            elif character == "\n":
                frames.extend(self._consume_line())
            else:
                self._line += character
        return frames

    def finish(self) -> list[ParsedSSEFrame]:
        """Discard an event that was not terminated by a blank line at EOF."""
        self._saw_cr = False
        if self._line:
            self._consume_line()
        if self._has_fields or self._data_lines:
            self._dispatch()
        return []

    def _consume_line(self) -> list[ParsedSSEFrame]:
        line, self._line = self._line, ""
        if line == "":
            if self._has_fields or self._data_lines:
                return [self._dispatch()]
            return []
        if line.startswith(":"):
            return []
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        elif not separator:
            value = ""
        self._has_fields = True
        if field == "data":
            self._data_lines.append(value)
        elif field == "id" and "\x00" not in value:
            self._event_id = value
        elif field == "event":
            self._event = value
        elif field == "retry" and value.isdigit():
            self._retry = int(value)
        return []

    def _dispatch(self) -> ParsedSSEFrame:
        frame = ParsedSSEFrame(
            data="\n".join(self._data_lines) if self._data_lines else None,
            event_id=self._event_id,
            event=self._event,
            retry=self._retry,
        )
        self._data_lines = []
        self._event_id = None
        self._event = None
        self._retry = None
        self._has_fields = False
        return frame


def _occurred_at(value: Any = None) -> str:
    if value not in (None, ""):
        if isinstance(value, datetime):
            parsed = value if value.tzinfo else value.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        try:
            return datetime.fromtimestamp(float(value), tz=UTC).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OSError):
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    return parsed.astimezone(UTC).isoformat().replace(
                        "+00:00", "Z"
                    )
                except ValueError:
                    pass
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _legacy_payload(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    legacy_type = str(event.get("type") or "unknown")
    event_type = _LEGACY_EVENT_TYPES.get(legacy_type, f"legacy.{legacy_type}")
    payload = {key: value for key, value in event.items() if key != "type"}
    payload["legacy_type"] = legacy_type
    if legacy_type == "stream_start":
        payload.setdefault("phase", "running")
    elif legacy_type in ("done", "message_stop"):
        # Provider completion is not the fenced DB terminal transition.
        payload.setdefault("final_received", True)
        payload.setdefault("execution_terminal", False)
    elif legacy_type == "resume_done":
        payload.setdefault("replay_complete", True)
        payload.setdefault("execution_terminal", False)
    elif legacy_type == "resume_unavailable":
        payload.setdefault("snapshot_required", True)
        payload.setdefault("execution_terminal", False)
    elif legacy_type == "resume_timeout":
        payload.setdefault("execution_terminal", False)
    return event_type, payload


def _validate_v2_envelope(envelope: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "type",
        "occurred_at",
        "payload",
    }
    if missing := sorted(required.difference(envelope)):
        raise ChatProtocolError(
            "invalid_chat_event_envelope",
            f"v2 event envelope is missing required fields: {', '.join(missing)}",
        )
    schema_version = envelope.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != CHAT_EVENT_SCHEMA_VERSION
    ):
        raise ChatProtocolError(
            "unsupported_chat_event_schema_version",
            f"chat event schema version must be {CHAT_EVENT_SCHEMA_VERSION}",
        )
    if not isinstance(envelope.get("type"), str) or not envelope["type"].strip():
        raise ChatProtocolError(
            "invalid_chat_event_envelope",
            "v2 event envelope requires a non-empty string type",
        )
    if not isinstance(envelope.get("payload"), dict):
        raise ChatProtocolError(
            "invalid_chat_event_envelope",
            "v2 event envelope requires an object payload",
        )
    event_id = envelope.get("event_id")
    if event_id is not None and (
        not isinstance(event_id, str) or not event_id.strip()
    ):
        raise ChatProtocolError(
            "invalid_chat_event_envelope",
            "v2 event_id must be a non-empty opaque string or null",
        )
    for field in ("session_id", "execution_id", "generation_id", "segment_id"):
        value = envelope.get(field)
        if value is None:
            continue
        try:
            UUID(str(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ChatProtocolError(
                "invalid_chat_event_envelope",
                f"v2 {field} must be a UUID or null",
            ) from exc
    for field in ("owner_epoch", "sequence"):
        value = envelope.get(field)
        if value is not None and (
            not isinstance(value, str) or not value.isdigit()
        ):
            raise ChatProtocolError(
                "invalid_chat_event_envelope",
                f"v2 {field} must be a decimal string or null",
            )
    occurred_at = envelope.get("occurred_at")
    if not isinstance(occurred_at, str):
        raise ChatProtocolError(
            "invalid_chat_event_envelope",
            "v2 occurred_at must be an ISO-8601 string",
        )
    try:
        datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChatProtocolError(
            "invalid_chat_event_envelope",
            "v2 occurred_at must be an ISO-8601 string",
        ) from exc


def build_event_envelope(
    event: dict[str, Any],
    *,
    event_id: str | None,
    session_id: str | None,
    execution_id: str | None,
    owner_epoch: int | str | None = None,
    generation_id: str | None = None,
    segment_id: str | None = None,
    sequence: int | str | None = None,
    occurred_at: Any = None,
) -> dict[str, Any]:
    """Adapt one legacy event to the v2 envelope without inventing terminal state."""
    if "schema_version" in event:
        event_schema_version = event.get("schema_version")
        if (
            not isinstance(event_schema_version, int)
            or isinstance(event_schema_version, bool)
            or event_schema_version != CHAT_EVENT_SCHEMA_VERSION
        ):
            raise ChatProtocolError(
                "unsupported_chat_event_schema_version",
                f"chat event schema version must be {CHAT_EVENT_SCHEMA_VERSION}",
            )
        envelope = dict(event)
        existing_event_id = event.get("event_id")
        if existing_event_id and event_id and str(existing_event_id) != str(event_id):
            raise ChatProtocolError(
                "chat_event_id_mismatch",
                "SSE id and envelope event_id do not match",
            )
        if event_id:
            envelope["event_id"] = str(event_id)
        for field, context_value in (
            ("session_id", session_id),
            ("execution_id", execution_id),
            ("owner_epoch", owner_epoch),
            ("generation_id", generation_id),
            ("segment_id", segment_id),
            ("sequence", sequence),
        ):
            existing_scope = event.get(field)
            if (
                existing_scope not in (None, "")
                and context_value is not None
                and str(existing_scope) != str(context_value)
            ):
                raise ChatProtocolError(
                    "chat_event_scope_mismatch",
                    f"v2 event {field} does not match the stream scope",
                )
            if context_value is not None:
                envelope[field] = str(context_value)
        _validate_v2_envelope(envelope)
        return envelope

    event_type, payload = _legacy_payload(event)
    resolved_execution_id = execution_id or event.get("execution_id")
    resolved_session_id = session_id or event.get("session_id")
    resolved_generation_id = generation_id or event.get("generation_id")
    resolved_segment_id = segment_id or event.get("segment_id")
    message = event.get("message")
    if not resolved_segment_id and isinstance(message, dict):
        resolved_segment_id = message.get("id")
    resolved_owner_epoch = (
        owner_epoch if owner_epoch is not None else event.get("owner_epoch")
    )
    envelope = {
        "schema_version": CHAT_EVENT_SCHEMA_VERSION,
        "event_id": str(event_id) if event_id else None,
        "session_id": str(resolved_session_id) if resolved_session_id else None,
        "execution_id": str(resolved_execution_id) if resolved_execution_id else None,
        "owner_epoch": str(resolved_owner_epoch)
        if resolved_owner_epoch is not None
        else None,
        "generation_id": str(resolved_generation_id) if resolved_generation_id else None,
        "segment_id": str(resolved_segment_id) if resolved_segment_id else None,
        "sequence": str(sequence) if sequence is not None else None,
        "type": event_type,
        "occurred_at": _occurred_at(occurred_at),
        "payload": payload,
    }
    _validate_v2_envelope(envelope)
    return envelope


def encode_v2_sse_event(
    raw_sse: str,
    *,
    event_id: str | None = None,
    session_id: str | None = None,
    execution_id: str | None = None,
    owner_epoch: int | str | None = None,
    generation_id: str | None = None,
    sequence: int | str | None = None,
    occurred_at: Any = None,
) -> str:
    """Convert exactly one legacy SSE data frame into a v2 SSE data frame."""
    decoder = SSEFrameDecoder()
    frames = decoder.feed(raw_sse)
    frames.extend(decoder.finish())
    data_frames = [frame for frame in frames if frame.data is not None]
    if len(data_frames) != 1:
        raise ChatProtocolError(
            "invalid_legacy_sse_frame",
            "one persisted event id must contain exactly one SSE data frame",
        )
    frame = data_frames[0]
    resolved_event_id = event_id or frame.event_id
    if event_id and frame.event_id and str(event_id) != str(frame.event_id):
        raise ChatProtocolError(
            "chat_event_id_mismatch",
            "persisted event id and SSE id do not match",
        )
    try:
        event = json.loads(frame.data or "")
    except json.JSONDecodeError as exc:
        raise ChatProtocolError(
            "invalid_legacy_event_json", "legacy event is not valid JSON"
        ) from exc
    if not isinstance(event, dict):
        raise ChatProtocolError("invalid_legacy_event_json", "legacy event must be a JSON object")
    envelope = build_event_envelope(
        event,
        event_id=resolved_event_id,
        session_id=session_id,
        execution_id=execution_id,
        owner_epoch=owner_epoch,
        generation_id=generation_id,
        sequence=sequence,
        occurred_at=occurred_at,
    )
    prefix = f"id:{resolved_event_id}\n" if resolved_event_id else ""
    return f"{prefix}data:{json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))}\n\n"


def encode_snapshot_required_event(
    *,
    reason: str,
    session_id: str | None,
    execution_id: str | None,
    server_high_watermark: str | None = None,
    failed_event_id: str | None = None,
) -> str:
    """Emit a valid recovery instruction without advancing the applied cursor."""
    envelope = build_event_envelope(
        {
            "type": "resume_unavailable",
            "reason": reason,
            "snapshot_required": True,
            "server_high_watermark": server_high_watermark,
            "failed_event_id": failed_event_id,
        },
        event_id=None,
        session_id=session_id,
        execution_id=execution_id,
    )
    envelope["type"] = "stream.snapshot_required"
    return f"data:{json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def adapt_sse_stream(
    source: AsyncIterable[str],
    *,
    session_id: str | None,
    execution_id: str | None = None,
    owner_epoch: int | str | None = None,
) -> AsyncGenerator[str, None]:
    """Adapt a live v1 stream to v2 with one shared incremental parser."""
    decoder = SSEFrameDecoder()
    current_execution_id = execution_id

    async def _adapt_frame(frame: ParsedSSEFrame) -> str | None:
        nonlocal current_execution_id
        if frame.data is None:
            if frame.retry is not None:
                return f"retry:{frame.retry}\n\n"
            return None
        try:
            event = json.loads(frame.data)
            if not isinstance(event, dict):
                raise TypeError("event must be an object")
        except (json.JSONDecodeError, TypeError):
            return encode_snapshot_required_event(
                reason="invalid_event_frame",
                session_id=session_id,
                execution_id=current_execution_id,
                failed_event_id=frame.event_id,
            )
        if event.get("type") == "stream_start" and event.get("execution_id"):
            current_execution_id = str(event["execution_id"])
        try:
            replayable_frame = "".join(f"data:{line}\n" for line in frame.data.split("\n")) + "\n"
            return encode_v2_sse_event(
                replayable_frame,
                event_id=frame.event_id,
                session_id=session_id,
                execution_id=current_execution_id,
                owner_epoch=owner_epoch,
            )
        except ChatProtocolError:
            return encode_snapshot_required_event(
                reason="invalid_event_envelope",
                session_id=session_id,
                execution_id=current_execution_id,
                failed_event_id=frame.event_id,
            )

    async for chunk in source:
        for frame in decoder.feed(chunk):
            adapted = await _adapt_frame(frame)
            if adapted:
                yield adapted
                if '"type":"stream.snapshot_required"' in adapted:
                    return
    for frame in decoder.finish():
        adapted = await _adapt_frame(frame)
        if adapted:
            yield adapted
            if '"type":"stream.snapshot_required"' in adapted:
                return


def _version_from_datetime(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return str(int(value.timestamp() * 1_000_000))


def _normalize_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except json.JSONDecodeError:
            return []
    return []


async def get_stream_snapshot(
    *,
    session_id: UUID,
    tenant_id: UUID,
    execution_id: UUID | None = None,
    last_applied_event_id: str = "0",
) -> dict[str, Any] | None:
    """Read a tenant-scoped DB snapshot and its independent Redis watermark.

    This query is deliberately side-effect free.  In particular it does not use
    the legacy status/list repair paths and never claims or releases an execution
    lease. With the WP04 migration gate enabled, content and coverage come from
    one checkpoint row; Redis's last id remains only an advertisement that newer
    events may exist.
    """
    from app.core.db_pool import get_pool

    applied_cursor = validate_event_cursor(last_applied_event_id) or "0"
    async with get_pool().acquire() as conn:
        migration_ready = os.getenv(
            "AADS_CHAT_WP04_MIGRATION_READY", "false"
        ).lower() in {"1", "true", "yes", "on"}
        if migration_ready:
            row = await conn.fetchrow(
                """
                SELECT s.id AS session_id,
                       s.message_count,
                       s.updated_at AS session_updated_at,
                       COALESCE(r.revision, 0)::text AS ledger_revision,
                       te.id AS execution_id,
                       te.status AS execution_phase,
                       te.owner_epoch,
                       cp.covers_through_event_id,
                       COALESCE(cp.message_id, m.id) AS message_id,
                       CASE WHEN cp.execution_id IS NOT NULL
                            THEN cp.partial_content ELSE COALESCE(m.content, '') END AS content,
                       COALESCE(cp.intent, m.intent) AS intent,
                       CASE WHEN cp.execution_id IS NOT NULL
                            THEN cp.tool_checkpoint ELSE COALESCE(m.tools_called, '[]'::jsonb)
                       END AS tools_called,
                       cp.generation_id AS checkpoint_generation_id,
                       cp.segment_id AS checkpoint_segment_id,
                       cp.content_version::text AS checkpoint_content_version,
                       m.created_at AS message_created_at,
                       COALESCE(m.edited_at, m.created_at) AS message_edited_at
                FROM chat_sessions s
                LEFT JOIN chat_session_revisions r
                  ON r.tenant_id = s.tenant_id AND r.session_id = s.id
                LEFT JOIN LATERAL (
                    SELECT candidate.*
                    FROM chat_turn_executions candidate
                    WHERE candidate.session_id = s.id
                      AND ($3::uuid IS NULL OR candidate.id = $3::uuid)
                    ORDER BY
                        CASE WHEN candidate.status IN ('running', 'retrying') THEN 0 ELSE 1 END,
                        candidate.updated_at DESC,
                        candidate.id DESC
                    LIMIT 1
                ) te ON TRUE
                LEFT JOIN chat_execution_checkpoints cp
                  ON cp.execution_id = te.id
                 AND cp.tenant_id = s.tenant_id
                 AND cp.session_id = s.id
                LEFT JOIN chat_messages m
                  ON m.id = te.assistant_message_id
                 AND m.tenant_id = s.tenant_id
                 AND m.session_id = s.id
                WHERE s.id = $1
                  AND s.tenant_id = $2
                """,
                session_id,
                tenant_id,
                execution_id,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT s.id AS session_id,
                       s.message_count,
                       s.updated_at AS session_updated_at,
                       te.id AS execution_id,
                       te.status AS execution_phase,
                       te.owner_epoch,
                       te.last_event_id AS covers_through_event_id,
                       m.id AS message_id,
                       m.content,
                       m.intent,
                       m.tools_called,
                       m.created_at AS message_created_at,
                       COALESCE(
                           NULLIF(to_jsonb(m)->>'edited_at', '')::timestamptz,
                           m.created_at
                       ) AS message_edited_at
                FROM chat_sessions s
                LEFT JOIN LATERAL (
                    SELECT candidate.*
                    FROM chat_turn_executions candidate
                    WHERE candidate.session_id = s.id
                      AND ($3::uuid IS NULL OR candidate.id = $3::uuid)
                    ORDER BY
                        CASE WHEN candidate.status IN ('running', 'retrying') THEN 0 ELSE 1 END,
                        candidate.updated_at DESC,
                        candidate.id DESC
                    LIMIT 1
                ) te ON TRUE
                LEFT JOIN LATERAL (
                    SELECT candidate_message.*
                    FROM chat_messages candidate_message
                    WHERE candidate_message.execution_id = te.id
                      AND candidate_message.role = 'assistant'
                    ORDER BY
                        CASE
                            WHEN candidate_message.id = te.assistant_message_id THEN 0
                            WHEN candidate_message.intent = 'streaming_placeholder' THEN 1
                            ELSE 2
                        END,
                        candidate_message.created_at DESC,
                        candidate_message.id DESC
                    LIMIT 1
                ) m ON TRUE
                WHERE s.id = $1
                  AND s.tenant_id = $2
                """,
                session_id,
                tenant_id,
                execution_id,
            )
    if not row:
        return None
    values = dict(row)
    resolved_execution_id = values.get("execution_id")
    if execution_id is not None and resolved_execution_id is None:
        return None

    covers = values.get("covers_through_event_id")
    covers = str(covers) if covers else None
    stream_id = str(resolved_execution_id or session_id)
    stream_info = await redis_stream.get_stream_info(stream_id)
    high_watermark = None
    first_available = None
    retention_trimmed = None
    max_deleted_event_id = None
    if stream_info:
        high_watermark = stream_info.get("last_event_id")
        first_available = stream_info.get("first_event_id")
        retention_trimmed = stream_info.get("retention_trimmed")
        max_deleted_event_id = stream_info.get("max_deleted_event_id")
    high_watermark = str(high_watermark) if high_watermark else covers
    first_available = str(first_available) if first_available else None

    coverage_mismatch = bool(
        covers and high_watermark and compare_redis_event_ids(covers, high_watermark) > 0
    )
    if retention_trimmed is True and (max_deleted_event_id or first_available):
        coverage_cursor = covers or "0"
        retention_floor = str(max_deleted_event_id or first_available)
        if compare_redis_event_ids(coverage_cursor, retention_floor) < 0:
            coverage_mismatch = True
    replay_required = bool(
        high_watermark
        and compare_redis_event_ids(covers or "0", high_watermark) < 0
        and not coverage_mismatch
    )
    if stream_info:
        replay_status = "coverage_mismatch" if coverage_mismatch else "available"
    elif covers or resolved_execution_id:
        replay_status = "unavailable"
    else:
        replay_status = "not_started"

    session_version = _version_from_datetime(values.get("session_updated_at")) or "0"
    content_version = values.get("checkpoint_content_version") or _version_from_datetime(
        values.get("message_edited_at") or values.get("message_created_at")
    )
    message_count = int(values.get("message_count") or 0)
    message_id = values.get("message_id")
    snapshot_required = bool(
        coverage_mismatch
        or (covers and applied_cursor != covers)
        or (not covers and applied_cursor != "0")
    )
    return {
        "schema_version": CHAT_EVENT_SCHEMA_VERSION,
        "contract_version": CHAT_CONTRACT_V2,
        "session_id": str(session_id),
        "session_revision": str(
            values.get("ledger_revision") or f"{message_count}:{session_version}"
        ),
        "execution_id": str(resolved_execution_id) if resolved_execution_id else None,
        "generation_id": str(values["checkpoint_generation_id"])
        if values.get("checkpoint_generation_id")
        else None,
        "segment_id": str(values.get("checkpoint_segment_id") or message_id)
        if values.get("checkpoint_segment_id") or message_id
        else None,
        "message_id": str(message_id) if message_id else None,
        "content_version": content_version,
        "content_completeness": (
            "partial"
            if values.get("intent")
            in {"streaming_placeholder", "interrupted_partial", "_archived_partial"}
            else "full"
        ),
        "content": str(values.get("content") or ""),
        "intent": values.get("intent"),
        "tools_called": _normalize_json_list(values.get("tools_called")),
        "execution_phase": str(values.get("execution_phase") or "idle"),
        "owner_epoch": str(values["owner_epoch"])
        if values.get("owner_epoch") is not None
        else None,
        "covers_through_event_id": covers,
        "server_high_watermark": high_watermark,
        "first_available_event_id": first_available,
        "retention_trimmed": retention_trimmed,
        "max_deleted_event_id": str(max_deleted_event_id)
        if max_deleted_event_id
        else None,
        "last_applied_event_id": applied_cursor,
        "resume_from_event_id": covers or "0",
        "snapshot_required": snapshot_required,
        "replay_required": replay_required,
        "replay_status": replay_status,
        "coverage_mismatch": coverage_mismatch,
    }
