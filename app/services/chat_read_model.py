"""WP04 tenant-scoped, read-only chat projections and signed message cursors.

The legacy chat service contains recovery helpers because old GET requests also
repaired database rows.  WP04 keeps those helpers for command/worker callers,
but routes reads through this module.  Every database transaction opened here
is explicitly read-only and every cursor is bound to its complete authorization
and projection scope.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Any
from uuid import UUID


CHAT_CURSOR_SCHEMA_VERSION = 2
DEFAULT_CHAT_CURSOR_TTL_SECONDS = 15 * 60
MAX_CHAT_CURSOR_TTL_SECONDS = 24 * 60 * 60
_CURSOR_SECRET_ENV = "AADS_CHAT_CURSOR_HMAC_SECRET"
_CURSOR_TTL_ENV = "AADS_CHAT_CURSOR_TTL_SECONDS"
_PROJECTIONS = frozenset({"minimal", "render", "full"})
_DIRECTIONS = frozenset({"before", "after"})


class ChatReadModelError(ValueError):
    """Stable error raised for v2 read-model contract failures."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _pool():
    # Import lazily so tests and application startup use the same pool seam as
    # the legacy chat service.
    from app.core.db_pool import get_pool

    return get_pool()


@asynccontextmanager
async def _read_only_transaction(conn: Any, *, repeatable: bool = False):
    """Open an asyncpg read-only transaction and tolerate awaitable test doubles."""
    transaction = conn.transaction(
        isolation="repeatable_read" if repeatable else None,
        readonly=True,
    )
    if isawaitable(transaction):
        transaction = await transaction
    async with transaction:
        yield


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _cursor_secret(secret: str | bytes | None = None) -> bytes:
    raw = secret if secret is not None else os.getenv(_CURSOR_SECRET_ENV, "")
    value = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
    if len(value) < 32:
        raise ChatReadModelError(
            "chat_cursor_secret_unavailable",
            f"{_CURSOR_SECRET_ENV} must contain at least 32 bytes",
            status_code=503,
        )
    return value


def chat_cursor_secret_configured() -> bool:
    """Return only secret readiness; never expose or fingerprint the secret."""
    try:
        _cursor_secret()
    except ChatReadModelError:
        return False
    return True


def chat_cursor_ttl_seconds() -> int:
    try:
        configured = int(os.getenv(_CURSOR_TTL_ENV, str(DEFAULT_CHAT_CURSOR_TTL_SECONDS)))
    except ValueError:
        configured = DEFAULT_CHAT_CURSOR_TTL_SECONDS
    return max(60, min(configured, MAX_CHAT_CURSOR_TTL_SECONDS))


def wp04_read_model_activation_ready() -> bool:
    """Require every WP04 operational gate without exposing secret material."""
    enabled = os.getenv("AADS_CHAT_V2_READ_MODEL_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    migration_ready = os.getenv(
        "AADS_CHAT_WP04_MIGRATION_READY", "false"
    ).lower() in {"1", "true", "yes", "on"}
    cross_version_ready = os.getenv(
        "AADS_CHAT_WP04_CROSS_VERSION_READY", "false"
    ).lower() in {"1", "true", "yes", "on"}
    return bool(
        enabled
        and migration_ready
        and cross_version_ready
        and chat_cursor_secret_configured()
    )


def _normalize_created_at(value: datetime | str) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ChatReadModelError(
                "invalid_chat_message_cursor",
                "cursor created_at must be an ISO-8601 timestamp",
            ) from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ChatReadModelError(
            "invalid_chat_message_cursor",
            "cursor created_at must be an ISO-8601 timestamp",
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _cursor_scope(
    *,
    tenant_id: UUID | str,
    user_id: str,
    session_id: UUID | str,
    projection: str,
    include_streaming: bool,
    direction: str,
) -> dict[str, Any]:
    if projection not in _PROJECTIONS:
        raise ChatReadModelError("invalid_chat_projection", "unsupported chat projection")
    if direction not in _DIRECTIONS:
        raise ChatReadModelError("invalid_chat_cursor_direction", "direction must be before or after")
    try:
        normalized = {
            "tenant_id": str(UUID(str(tenant_id))),
            "session_id": str(UUID(str(session_id))),
        }
    except (TypeError, ValueError, AttributeError) as exc:
        raise ChatReadModelError(
            "invalid_chat_cursor_scope", "tenant and session cursor scope must use UUIDs"
        ) from exc
    normalized_user_id = "" if user_id is None else str(user_id).strip()
    if not normalized_user_id or len(normalized_user_id) > 200:
        raise ChatReadModelError(
            "invalid_chat_cursor_scope", "user cursor scope is invalid"
        )
    return {
        **normalized,
        "user_id": normalized_user_id,
        "projection": projection,
        "include_streaming": bool(include_streaming),
        "direction": direction,
    }


def encode_message_cursor(
    *,
    created_at: datetime | str,
    message_id: UUID | str,
    tenant_id: UUID | str,
    user_id: str,
    session_id: UUID | str,
    projection: str,
    include_streaming: bool,
    direction: str,
    now: int | float | None = None,
    ttl_seconds: int | None = None,
    secret: str | bytes | None = None,
) -> str:
    """Sign a `(created_at,id)` keyset and its complete request scope."""
    issued_at = int(time.time() if now is None else now)
    ttl = chat_cursor_ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
    if ttl < 1 or ttl > MAX_CHAT_CURSOR_TTL_SECONDS:
        raise ChatReadModelError("invalid_chat_cursor_ttl", "cursor TTL is outside the allowed range")
    scope = _cursor_scope(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        projection=projection,
        include_streaming=include_streaming,
        direction=direction,
    )
    try:
        normalized_message_id = str(UUID(str(message_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ChatReadModelError(
            "invalid_chat_message_cursor", "cursor message id must be a UUID"
        ) from exc
    payload = {
        "v": CHAT_CURSOR_SCHEMA_VERSION,
        "iat": issued_at,
        "exp": issued_at + ttl,
        **scope,
        "created_at": _normalize_created_at(created_at),
        "id": normalized_message_id,
    }
    encoded_payload = _b64url_encode(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    signature = hmac.new(_cursor_secret(secret), encoded_payload.encode("ascii"), hashlib.sha256)
    return f"{encoded_payload}.{_b64url_encode(signature.digest())}"


def decode_message_cursor(
    cursor: str,
    *,
    tenant_id: UUID | str,
    user_id: str,
    session_id: UUID | str,
    projection: str,
    include_streaming: bool,
    direction: str,
    now: int | float | None = None,
    secret: str | bytes | None = None,
) -> tuple[datetime, UUID]:
    """Verify signature, TTL, schema, and scope before returning the keyset."""
    try:
        encoded_payload, encoded_signature = str(cursor).split(".", 1)
        supplied_signature = _b64url_decode(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise ChatReadModelError(
            "invalid_chat_message_cursor", "chat message cursor has an invalid format"
        ) from exc
    expected_signature = hmac.new(
        _cursor_secret(secret), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ChatReadModelError(
            "invalid_chat_message_cursor", "chat message cursor signature is invalid"
        )
    try:
        payload = json.loads(_b64url_decode(encoded_payload))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatReadModelError(
            "invalid_chat_message_cursor", "chat message cursor payload is invalid"
        ) from exc
    if not isinstance(payload, dict) or payload.get("v") != CHAT_CURSOR_SCHEMA_VERSION:
        raise ChatReadModelError(
            "unsupported_chat_message_cursor", "chat message cursor version is not supported"
        )
    if not isinstance(payload.get("include_streaming"), bool):
        raise ChatReadModelError(
            "invalid_chat_message_cursor",
            "chat message cursor filter scope is invalid",
        )
    expected_scope = _cursor_scope(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        projection=projection,
        include_streaming=include_streaming,
        direction=direction,
    )
    if any(payload.get(field) != value for field, value in expected_scope.items()):
        raise ChatReadModelError(
            "chat_message_cursor_scope_mismatch",
            "chat message cursor does not belong to this request scope",
        )
    current_time = int(time.time() if now is None else now)
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_CHAT_CURSOR_TTL_SECONDS
        or issued_at > current_time + 60
    ):
        raise ChatReadModelError(
            "invalid_chat_message_cursor", "chat message cursor timestamps are invalid"
        )
    if current_time >= expires_at:
        raise ChatReadModelError("expired_chat_message_cursor", "chat message cursor has expired")
    try:
        created_at = datetime.fromisoformat(str(payload["created_at"]).replace("Z", "+00:00"))
        message_id = UUID(str(payload["id"]))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ChatReadModelError(
            "invalid_chat_message_cursor", "chat message cursor keyset is invalid"
        ) from exc
    if created_at.tzinfo is None:
        raise ChatReadModelError(
            "invalid_chat_message_cursor", "chat message cursor timestamp requires a timezone"
        )
    return created_at.astimezone(UTC), message_id


async def _schema_ready(conn: Any) -> bool:
    row = await conn.fetchrow(
        """
        SELECT to_regclass('chat_session_revisions') IS NOT NULL AS revisions,
               to_regclass('chat_outbox') IS NOT NULL AS outbox,
               to_regclass('chat_execution_checkpoints') IS NOT NULL AS checkpoints,
               EXISTS (
                   SELECT 1
                   FROM information_schema.columns
                   WHERE table_schema = current_schema()
                     AND table_name = 'chat_messages'
                     AND column_name = 'content_version'
               ) AS content_version
        """
    )
    return bool(
        row
        and row["revisions"]
        and row["outbox"]
        and row["checkpoints"]
        and row["content_version"]
    )


async def _require_schema(conn: Any) -> None:
    if not await _schema_ready(conn):
        raise ChatReadModelError(
            "chat_read_model_migration_required",
            "the additive WP04 chat read-model migration is not available",
            status_code=503,
        )


def _projection_fields(projection: str) -> str:
    from app.services import chat_service as svc

    selected = svc._message_select_fields(projection)
    if selected == "*":
        return selected
    return (
        selected
        + ", content_version, generation_id, segment_id, deleted_at"
    )


async def _load_v2_page(
    conn: Any,
    *,
    session_id: UUID,
    tenant_id: UUID,
    user_id: str,
    limit: int,
    cursor: str | None,
    direction: str,
    include_streaming: bool,
    projection: str,
) -> dict[str, Any]:
    from app.services import chat_service as svc

    if projection not in _PROJECTIONS:
        raise ChatReadModelError("invalid_chat_projection", "unsupported chat projection")
    if direction not in _DIRECTIONS:
        raise ChatReadModelError("invalid_chat_cursor_direction", "direction must be before or after")
    limit = max(1, min(int(limit), 200))
    session_row = await conn.fetchrow(
        """
        SELECT s.id,
               COALESCE(r.message_revision, 0)::text AS message_revision,
               COALESCE(r.revision, 0)::text AS session_revision,
               EXISTS (
                   SELECT 1
                   FROM chat_turn_executions te
                   WHERE te.session_id = s.id
                     AND te.status IN ('running', 'retrying')
                     AND te.completed_at IS NULL
               ) AS is_active
        FROM chat_sessions s
        LEFT JOIN chat_session_revisions r
          ON r.session_id = s.id AND r.tenant_id = s.tenant_id
        WHERE s.id = $1
          AND s.tenant_id = $2
          AND (s.user_id IS NULL OR s.user_id = $3::text)
        """,
        session_id,
        tenant_id,
        user_id,
    )
    if not session_row:
        raise ChatReadModelError("chat_session_not_found", "chat session not found", status_code=404)

    cursor_created_at: datetime | None = None
    cursor_message_id: UUID | None = None
    if cursor:
        cursor_created_at, cursor_message_id = decode_message_cursor(
            cursor,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            projection=projection,
            include_streaming=include_streaming,
            direction=direction,
        )

    visibility = svc._visible_message_filter(bool(session_row["is_active"]), include_streaming)
    selected = _projection_fields(projection)
    fetch_limit = limit + 1
    if cursor_created_at is None and direction == "before":
        rows = await conn.fetch(
            "SELECT * FROM ("
            f" SELECT {selected} FROM chat_messages"
            " WHERE session_id = $1 AND tenant_id = $2 AND deleted_at IS NULL"
            f" {visibility}"
            " ORDER BY created_at DESC, id DESC LIMIT $3"
            ") page ORDER BY created_at ASC, id ASC",
            session_id,
            tenant_id,
            fetch_limit,
        )
        effective_direction = "before"
    elif cursor_created_at is None:
        rows = await conn.fetch(
            f"SELECT {selected} FROM chat_messages"
            " WHERE session_id = $1 AND tenant_id = $2 AND deleted_at IS NULL"
            f" {visibility}"
            " ORDER BY created_at ASC, id ASC LIMIT $3",
            session_id,
            tenant_id,
            fetch_limit,
        )
        effective_direction = "after"
    elif direction == "before":
        rows = await conn.fetch(
            "SELECT * FROM ("
            f" SELECT {selected} FROM chat_messages"
            " WHERE session_id = $1 AND tenant_id = $2 AND deleted_at IS NULL"
            f" {visibility}"
            " AND (created_at, id) < ($3::timestamptz, $4::uuid)"
            " ORDER BY created_at DESC, id DESC LIMIT $5"
            ") page ORDER BY created_at ASC, id ASC",
            session_id,
            tenant_id,
            cursor_created_at,
            cursor_message_id,
            fetch_limit,
        )
        effective_direction = direction
    else:
        rows = await conn.fetch(
            f"SELECT {selected} FROM chat_messages"
            " WHERE session_id = $1 AND tenant_id = $2 AND deleted_at IS NULL"
            f" {visibility}"
            " AND (created_at, id) > ($3::timestamptz, $4::uuid)"
            " ORDER BY created_at ASC, id ASC LIMIT $5",
            session_id,
            tenant_id,
            cursor_created_at,
            cursor_message_id,
            fetch_limit,
        )
        effective_direction = direction

    raw_messages = [svc._row_to_dict(row) for row in rows]
    has_more = len(raw_messages) > limit
    if has_more:
        if effective_direction == "before":
            raw_messages = raw_messages[1:]
        else:
            raw_messages = raw_messages[:-1]
    boundary = None
    if has_more and raw_messages:
        boundary = raw_messages[0] if effective_direction == "before" else raw_messages[-1]

    messages = await svc._hydrate_message_response_durations(
        conn, raw_messages, persist=False
    )
    if projection != "minimal":
        if projection != "render":
            messages = [svc._apply_tool_summary(message) for message in messages]
        messages = await svc._repair_completed_execution_message_flags(
            conn,
            messages,
            "chat_read_model",
            persist=False,
        )
        if not bool(session_row["is_active"]):
            messages = svc._project_inactive_streaming_placeholders(messages)
        messages = svc._dedupe_recovery_like_messages(messages)

    for message in messages:
        content_version = message.get("content_version")
        message["content_version"] = str(content_version or 1)
        if projection == "minimal" and message.get("is_truncated"):
            message["content_completeness"] = "preview"
        elif message.get("intent") in {
            "streaming_placeholder",
            "interrupted_partial",
            "_archived_partial",
        }:
            message["content_completeness"] = "partial"
        else:
            message["content_completeness"] = "full"
        message["generation_id"] = (
            str(message["generation_id"]) if message.get("generation_id") else None
        )
        message["segment_id"] = str(message.get("segment_id") or message["id"])

    next_cursor = None
    if boundary is not None:
        next_cursor = encode_message_cursor(
            created_at=boundary["created_at"],
            message_id=boundary["id"],
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            projection=projection,
            include_streaming=include_streaming,
            direction=effective_direction,
        )
    previous_cursor = None
    if raw_messages:
        opposite_direction = "after" if effective_direction == "before" else "before"
        opposite_boundary = (
            raw_messages[-1] if effective_direction == "before" else raw_messages[0]
        )
        previous_cursor = encode_message_cursor(
            created_at=opposite_boundary["created_at"],
            message_id=opposite_boundary["id"],
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            projection=projection,
            include_streaming=include_streaming,
            direction=opposite_direction,
        )
    return {
        "schema_version": CHAT_CURSOR_SCHEMA_VERSION,
        "contract_version": 2,
        "session_id": str(session_id),
        "projection": projection,
        "session_revision": str(session_row["session_revision"]),
        "message_revision": str(session_row["message_revision"]),
        "messages": messages,
        "page": {
            "direction": effective_direction,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "previous_cursor": previous_cursor,
        },
    }


async def list_messages_v2(
    *,
    session_id: UUID,
    tenant_id: UUID,
    user_id: str,
    limit: int = 40,
    cursor: str | None = None,
    direction: str = "before",
    include_streaming: bool = False,
    projection: str = "render",
) -> dict[str, Any]:
    """Return a typed v2 page under a repeatable, read-only transaction."""
    _cursor_secret()
    async with _pool().acquire() as conn:
        async with _read_only_transaction(conn, repeatable=True):
            await _require_schema(conn)
            return await _load_v2_page(
                conn,
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                limit=limit,
                cursor=cursor,
                direction=direction,
                include_streaming=include_streaming,
                projection=projection,
            )


async def get_message_v2(
    *,
    message_id: UUID,
    tenant_id: UUID,
    user_id: str,
    projection: str = "full",
) -> dict[str, Any] | None:
    """Hydrate one authorized message with a stable content-version contract."""
    from app.services import chat_service as svc

    _cursor_secret()
    if projection not in _PROJECTIONS:
        raise ChatReadModelError("invalid_chat_projection", "unsupported chat projection")
    async with _pool().acquire() as conn:
        async with _read_only_transaction(conn, repeatable=True):
            await _require_schema(conn)
            selected = _projection_fields(projection)
            row = await conn.fetchrow(
                f"""
                SELECT {selected}, EXISTS (
                    SELECT 1
                    FROM chat_turn_executions te
                    WHERE te.session_id = m.session_id
                      AND te.status IN ('running', 'retrying')
                      AND te.completed_at IS NULL
                ) AS wp04_session_active
                FROM chat_messages m
                WHERE m.id = $1
                  AND m.tenant_id = $2
                  AND m.deleted_at IS NULL
                  AND COALESCE(m.is_hidden, FALSE) = FALSE
                  AND m.intent IS DISTINCT FROM '_deleted_duplicate'
                  AND EXISTS (
                    SELECT 1
                    FROM chat_sessions s
                    WHERE s.id = m.session_id
                      AND s.tenant_id = m.tenant_id
                      AND (s.user_id IS NULL OR s.user_id = $3::text)
                  )
                LIMIT 1
                """,
                message_id,
                tenant_id,
                user_id,
            )
            if not row:
                return None
            message = svc._row_to_dict(row)
            session_active = bool(message.pop("wp04_session_active", False))
            hydrated = await svc._hydrate_message_response_durations(
                conn, [message], persist=False
            )
            if projection != "minimal":
                if projection != "render":
                    hydrated = [svc._apply_tool_summary(item) for item in hydrated]
                hydrated = await svc._repair_completed_execution_message_flags(
                    conn,
                    hydrated,
                    "chat_read_model_detail",
                    persist=False,
                )
                if not session_active:
                    hydrated = svc._project_inactive_streaming_placeholders(hydrated)
            if not hydrated:
                return None
            message = hydrated[0]
            message["schema_version"] = CHAT_CURSOR_SCHEMA_VERSION
            message["contract_version"] = 2
            message["content_version"] = str(message.get("content_version") or 1)
            if projection == "minimal" and message.get("is_truncated"):
                completeness = "preview"
            elif message.get("intent") in {
                "streaming_placeholder",
                "interrupted_partial",
                "_archived_partial",
            }:
                completeness = "partial"
            else:
                completeness = "full"
            message["content_completeness"] = completeness
            message["generation_id"] = (
                str(message["generation_id"]) if message.get("generation_id") else None
            )
            message["segment_id"] = str(message.get("segment_id") or message["id"])
            return message


async def get_session_view_v2(
    *,
    session_id: UUID,
    tenant_id: UUID,
    user_id: str,
    limit: int = 40,
    projection: str = "render",
    include_streaming: bool = True,
) -> dict[str, Any]:
    """Read session metadata, page, execution, and checkpoint at one DB snapshot."""
    _cursor_secret()
    async with _pool().acquire() as conn:
        async with _read_only_transaction(conn, repeatable=True):
            await _require_schema(conn)
            page = await _load_v2_page(
                conn,
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                limit=limit,
                cursor=None,
                direction="before",
                include_streaming=include_streaming,
                projection=projection,
            )
            state = await conn.fetchrow(
                """
                SELECT s.title,
                       transaction_timestamp() AS snapshot_at,
                       COALESCE(r.revision, 0)::text AS session_revision,
                       COALESCE(r.message_revision, 0)::text AS message_revision,
                       COALESCE(r.artifact_revision, 0)::text AS artifact_revision,
                       COALESCE(r.execution_revision, 0)::text AS execution_revision,
                       te.id AS execution_id,
                       te.status AS execution_phase,
                       te.owner_epoch,
                       cp.execution_id AS checkpoint_execution_id,
                       cp.generation_id,
                       cp.segment_id,
                       cp.content_version::text AS checkpoint_content_version,
                       cp.covers_through_event_id,
                       cp.updated_at AS checkpoint_updated_at
                FROM chat_sessions s
                LEFT JOIN chat_session_revisions r
                  ON r.session_id = s.id AND r.tenant_id = s.tenant_id
                LEFT JOIN LATERAL (
                    SELECT candidate.*
                    FROM chat_turn_executions candidate
                    WHERE candidate.session_id = s.id
                    ORDER BY
                        CASE WHEN candidate.status IN ('running', 'retrying') THEN 0 ELSE 1 END,
                        candidate.updated_at DESC,
                        candidate.id DESC
                    LIMIT 1
                ) te ON TRUE
                LEFT JOIN chat_execution_checkpoints cp
                  ON cp.execution_id = te.id
                 AND cp.session_id = s.id
                 AND cp.tenant_id = s.tenant_id
                WHERE s.id = $1
                  AND s.tenant_id = $2
                  AND (s.user_id IS NULL OR s.user_id = $3::text)
                """,
                session_id,
                tenant_id,
                user_id,
            )
    if not state:
        raise ChatReadModelError("chat_session_not_found", "chat session not found", status_code=404)

    execution_id = state["execution_id"]
    server_high_watermark = None
    if execution_id:
        try:
            from app.services import redis_stream

            stream_info = await redis_stream.get_stream_info(str(execution_id))
            if stream_info and stream_info.get("last_event_id"):
                server_high_watermark = str(stream_info["last_event_id"])
        except Exception:
            server_high_watermark = None
    checkpoint = None
    if state["checkpoint_execution_id"]:
        checkpoint = {
            "execution_id": str(state["checkpoint_execution_id"]),
            "generation_id": str(state["generation_id"]) if state["generation_id"] else None,
            "segment_id": str(state["segment_id"]) if state["segment_id"] else None,
            "content_version": state["checkpoint_content_version"],
            "covers_through_event_id": state["covers_through_event_id"],
            "updated_at": state["checkpoint_updated_at"],
        }
    return {
        "schema_version": CHAT_CURSOR_SCHEMA_VERSION,
        "contract_version": 2,
        "production_ready": wp04_read_model_activation_ready(),
        "snapshot_at": state["snapshot_at"],
        "session_id": str(session_id),
        "title": state["title"],
        "revisions": {
            "session": str(state["session_revision"]),
            "message": str(state["message_revision"]),
            "artifact": str(state["artifact_revision"]),
            "execution": str(state["execution_revision"]),
        },
        "execution": {
            "id": str(execution_id),
            "phase": str(state["execution_phase"] or "idle"),
            "owner_epoch": str(state["owner_epoch"]),
        }
        if execution_id
        else None,
        "checkpoint": checkpoint,
        "server_high_watermark": server_high_watermark,
        "messages": page,
    }


async def get_changes_v2(
    *,
    session_id: UUID,
    tenant_id: UUID,
    user_id: str,
    after_revision: str,
    limit: int = 100,
) -> dict[str, Any]:
    """Return bounded outbox metadata; payloads contain IDs, never message bodies."""
    if not str(after_revision).isdigit():
        raise ChatReadModelError("invalid_chat_revision", "after_revision must be a decimal string")
    after = int(after_revision)
    limit = max(1, min(int(limit), 500))
    async with _pool().acquire() as conn:
        async with _read_only_transaction(conn, repeatable=True):
            await _require_schema(conn)
            revision = await conn.fetchrow(
                """
                SELECT COALESCE(r.revision, 0) AS current_revision,
                       MIN(o.session_revision) AS oldest_outbox_revision
                FROM chat_sessions s
                LEFT JOIN chat_session_revisions r
                  ON r.session_id = s.id AND r.tenant_id = s.tenant_id
                LEFT JOIN chat_outbox o
                  ON o.session_id = s.id AND o.tenant_id = s.tenant_id
                WHERE s.id = $1
                  AND s.tenant_id = $2
                  AND (s.user_id IS NULL OR s.user_id = $3::text)
                GROUP BY r.revision
                """,
                session_id,
                tenant_id,
                user_id,
            )
            if not revision:
                raise ChatReadModelError(
                    "chat_session_not_found", "chat session not found", status_code=404
                )
            current = int(revision["current_revision"] or 0)
            oldest = revision["oldest_outbox_revision"]
            if after > current:
                raise ChatReadModelError(
                    "chat_revision_ahead", "after_revision is ahead of the session revision"
                )
            snapshot_required = bool(
                current > after
                and (oldest is None or after < max(0, int(oldest) - 1))
            )
            rows = []
            if not snapshot_required:
                rows = await conn.fetch(
                    """
                    SELECT event_id, session_revision, event_type, payload, created_at
                    FROM chat_outbox
                    WHERE tenant_id = $1
                      AND session_id = $2
                      AND session_revision > $3
                    ORDER BY session_revision ASC, event_id ASC
                    LIMIT $4
                    """,
                    tenant_id,
                    session_id,
                    after,
                    limit + 1,
                )
    has_more = len(rows) > limit
    rows = rows[:limit]
    changes = []
    changed_message_ids: list[str] = []
    tombstones: list[str] = []
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        message_id = payload.get("message_id")
        if message_id:
            (
                tombstones
                if payload.get("operation") == "DELETE" or payload.get("tombstone") is True
                else changed_message_ids
            ).append(
                str(message_id)
            )
        changes.append(
            {
                "event_id": str(row["event_id"]),
                "revision": str(row["session_revision"]),
                "type": row["event_type"],
                "payload": payload,
                "occurred_at": row["created_at"],
            }
        )
    next_revision = str(rows[-1]["session_revision"] if rows else after)
    return {
        "schema_version": CHAT_CURSOR_SCHEMA_VERSION,
        "contract_version": 2,
        "session_id": str(session_id),
        "after_revision": str(after),
        "current_revision": str(current),
        "next_after_revision": next_revision,
        "snapshot_required": snapshot_required,
        "has_more": has_more,
        "changed_message_ids": list(dict.fromkeys(changed_message_ids)),
        "tombstones": list(dict.fromkeys(tombstones)),
        "changes": changes,
    }


async def get_streaming_status_projection(
    *,
    session_id: UUID,
    tenant_id: UUID,
    cached_status: dict[str, Any] | None,
    has_live_runtime: bool,
    acked_completion_token: str | None,
) -> dict[str, Any]:
    """Project legacy streaming status without repairing any durable state."""
    from app.services import chat_service as svc

    async with _pool().acquire() as conn:
        async with _read_only_transaction(conn):
            row = await conn.fetchrow(
                """
                SELECT s.id AS session_id,
                       te.id::text AS execution_id,
                       te.status,
                       te.last_event_id,
                       te.owner_epoch,
                       (te.owner_instance IS NOT NULL AND te.lease_expires_at > NOW()) AS lease_valid,
                       am.id::text AS final_message_id,
                       am.intent AS final_message_intent,
                       am.model_used AS assistant_model_used,
                       COALESCE(am.is_hidden, FALSE) AS final_message_hidden,
                       COALESCE(pm.id, spm.id, am.id)::text AS placeholder_message_id,
                       spm.execution_id::text AS standalone_placeholder_execution_id,
                       CASE
                           WHEN te.status IN ('running', 'retrying') THEN COALESCE(pm.content, am.content, '')
                           ELSE COALESCE(am.content, pm.content, spm.content, '')
                       END AS partial_content,
                       CASE
                           WHEN te.status IN ('running', 'retrying') THEN COALESCE(pm.tools_called, am.tools_called, '[]'::jsonb)
                           ELSE COALESCE(am.tools_called, pm.tools_called, spm.tools_called, '[]'::jsonb)
                       END AS tools_called,
                       recovered.id::text AS recovered_message_id,
                       interrupted.id::text AS interrupted_execution_id,
                       interrupted.last_event_id AS interrupted_last_event_id,
                       COALESCE(interrupted.partial_content, '') AS interrupted_partial_content
                FROM chat_sessions s
                LEFT JOIN LATERAL (
                    SELECT candidate.*
                    FROM chat_turn_executions candidate
                    WHERE candidate.session_id = s.id
                      AND (
                        candidate.status IN ('running', 'retrying')
                        OR (
                            candidate.status = 'completed'
                            AND candidate.completed_at IS NOT NULL
                            AND candidate.updated_at > NOW() - INTERVAL '5 minutes'
                            AND NOT EXISTS (
                                SELECT 1
                                FROM chat_messages newer_user
                                WHERE newer_user.session_id = candidate.session_id
                                  AND newer_user.role = 'user'
                                  AND newer_user.id IS DISTINCT FROM candidate.user_message_id
                                  AND newer_user.created_at > candidate.completed_at
                            )
                        )
                      )
                    ORDER BY
                        CASE WHEN candidate.status IN ('running', 'retrying') THEN 0 ELSE 1 END,
                        candidate.updated_at DESC,
                        candidate.id DESC
                    LIMIT 1
                ) te ON TRUE
                LEFT JOIN chat_messages am ON am.id = te.assistant_message_id
                LEFT JOIN LATERAL (
                    SELECT candidate.id, candidate.content, candidate.tools_called
                    FROM chat_messages candidate
                    WHERE candidate.execution_id = te.id
                      AND candidate.intent = 'streaming_placeholder'
                    ORDER BY candidate.created_at DESC, candidate.id DESC
                    LIMIT 1
                ) pm ON TRUE
                LEFT JOIN LATERAL (
                    SELECT candidate.id, candidate.execution_id,
                           candidate.content, candidate.tools_called
                    FROM chat_messages candidate
                    WHERE candidate.session_id = s.id
                      AND candidate.tenant_id = s.tenant_id
                      AND candidate.intent = 'streaming_placeholder'
                      AND candidate.created_at > NOW() - INTERVAL '5 minutes'
                    ORDER BY candidate.created_at DESC, candidate.id DESC
                    LIMIT 1
                ) spm ON TRUE
                LEFT JOIN LATERAL (
                    SELECT candidate.id
                    FROM chat_messages candidate
                    WHERE candidate.session_id = s.id
                      AND candidate.model_used IN ('recovered', 'recovered_from_redis')
                      AND candidate.created_at > NOW() - INTERVAL '5 minutes'
                      AND COALESCE(candidate.is_hidden, FALSE) = FALSE
                      AND candidate.intent IS DISTINCT FROM '_deleted_duplicate'
                      AND left(ltrim(COALESCE(candidate.content, '')), 240)
                          NOT LIKE '%[Pipeline Runner]%'
                    ORDER BY candidate.created_at DESC, candidate.id DESC
                    LIMIT 1
                ) recovered ON TRUE
                LEFT JOIN LATERAL (
                    SELECT candidate.id, candidate.last_event_id,
                           COALESCE(candidate_message.content, '') AS partial_content
                    FROM chat_turn_executions candidate
                    LEFT JOIN chat_messages candidate_message
                      ON candidate_message.id = candidate.assistant_message_id
                    WHERE candidate.session_id = s.id
                      AND candidate.status = 'interrupted'
                      AND candidate.updated_at > NOW() - INTERVAL '30 minutes'
                    ORDER BY candidate.updated_at DESC, candidate.id DESC
                    LIMIT 1
                ) interrupted ON TRUE
                WHERE s.id = $1 AND s.tenant_id = $2
                """,
                session_id,
                tenant_id,
            )
            if not row:
                # Preserve the legacy SELECT sequence for callers/test doubles
                # where the session/execution join cannot yield a null execution
                # row. No branch below performs repair DML.
                placeholder = await conn.fetchrow(
                    """
                    SELECT id::text AS placeholder_message_id, content AS partial_content,
                           tools_called, execution_id::text AS execution_id
                    FROM chat_messages
                    WHERE session_id = $1
                      AND tenant_id = $2
                      AND intent = 'streaming_placeholder'
                      AND created_at > NOW() - INTERVAL '5 minutes'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    session_id,
                    tenant_id,
                )
                recovered = await conn.fetchrow(
                    f"""
                    SELECT id
                    FROM chat_messages
                    WHERE session_id = $1
                      AND tenant_id = $2
                      AND model_used IN ('recovered', 'recovered_from_redis')
                      AND created_at > NOW() - INTERVAL '5 minutes'
                      AND COALESCE(is_hidden, FALSE) = FALSE
                      AND intent IS DISTINCT FROM '_deleted_duplicate'
                      {svc._AUTO_MESSAGE_EXCLUDE_FILTER}
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    session_id,
                    tenant_id,
                )
                interrupted = await conn.fetchrow(
                    """
                    SELECT te.id::text AS execution_id,
                           te.status,
                           te.last_event_id,
                           COALESCE(am.content, '') AS partial_content
                    FROM chat_turn_executions te
                    JOIN chat_sessions s ON s.id = te.session_id
                    LEFT JOIN chat_messages am ON am.id = te.assistant_message_id
                    WHERE te.session_id = $1
                      AND s.tenant_id = $2
                      AND te.status = 'interrupted'
                    ORDER BY te.updated_at DESC
                    LIMIT 1
                    """,
                    session_id,
                    tenant_id,
                )
                if placeholder:
                    placeholder_values = dict(placeholder)
                    partial = str(placeholder_values.get("partial_content") or "")
                    tool_events = svc.normalize_tool_events(
                        placeholder_values.get("tools_called")
                    )
                    tool_uses = [
                        event for event in tool_events if event.get("type") == "tool_use"
                    ]
                    return {
                        "is_streaming": bool(has_live_runtime),
                        "just_completed": False,
                        **svc.stream_status_payload(
                            "generating" if has_live_runtime else "needs_continuation"
                        ),
                        "content_length": len(partial),
                        "partial_content": partial or None,
                        "tool_count": len(tool_uses),
                        "last_tool": str(tool_uses[-1].get("tool_name") or "")
                        if tool_uses
                        else "",
                        "execution_id": placeholder_values.get("execution_id"),
                        "placeholder_message_id": placeholder_values.get(
                            "placeholder_message_id"
                        ),
                        "placeholder_ready": True,
                        "repair_required": not has_live_runtime,
                    }
                if recovered:
                    recovered_values = dict(recovered)
                    recovered_id = str(
                        recovered_values.get("recovered_message_id")
                        or recovered_values.get("id")
                    )
                    completion_token = f"recovered:{recovered_id}"
                    emit = svc.should_emit_completion_signal(
                        str(session_id), completion_token, acked_completion_token
                    )
                    return {
                        "is_streaming": False,
                        "just_completed": emit,
                        "recovered": True,
                        "completion_token": completion_token if emit else None,
                        **svc.stream_status_payload("completed"),
                        "final_message_id": recovered_id,
                        "final_message_ready": True,
                    }
                if interrupted:
                    interrupted_values = dict(interrupted)
                    partial = str(interrupted_values.get("partial_content") or "")
                    return {
                        "is_streaming": False,
                        "just_completed": False,
                        **svc.stream_status_payload("needs_continuation"),
                        "content_length": len(partial),
                        "partial_content": partial or None,
                        "execution_id": interrupted_values.get("execution_id"),
                        "last_event_id": interrupted_values.get("last_event_id"),
                    }
                return dict(
                    cached_status
                    or {"is_streaming": False, **svc.stream_status_payload("completed")}
                )
    values = dict(row)
    execution_id = values.get("execution_id")
    phase = str(values.get("status") or "")
    partial = str(values.get("partial_content") or "")
    tool_events = svc.normalize_tool_events(values.get("tools_called"))
    tool_uses = [event for event in tool_events if event.get("type") == "tool_use"]
    tool_count = len(tool_uses)
    last_tool = str(tool_uses[-1].get("tool_name") or "") if tool_uses else ""
    lease_live = bool(values.get("lease_valid"))
    if phase in {"running", "retrying"}:
        producer_live = bool(has_live_runtime or lease_live)
        recovering = phase == "retrying" or str(
            values.get("assistant_model_used") or ""
        ) in {"interrupted", "stopped"}
        status_name = (
            "recovering"
            if recovering and producer_live
            else ("tool_running" if producer_live and tool_count else "generating")
            if producer_live
            else "needs_continuation"
        )
        return {
            "is_streaming": producer_live,
            "just_completed": False,
            **svc.stream_status_payload(status_name),
            "content_length": len(partial),
            "partial_content": partial or None,
            "tool_count": tool_count,
            "last_tool": last_tool,
            "execution_id": execution_id,
            "last_event_id": values.get("last_event_id"),
            "placeholder_message_id": values.get("placeholder_message_id")
            or values.get("placeholder_id"),
            "placeholder_ready": bool(
                values.get("placeholder_message_id")
                or values.get("placeholder_id")
                or partial.strip()
            ),
            "final_message_id": values.get("final_message_id"),
            "final_message_ready": False,
            "repair_required": not producer_live,
        }
    if phase == "completed":
        final_ready = bool(
            values.get("final_message_id")
            and not values.get("final_message_hidden")
            and values.get("final_message_intent") != "streaming_placeholder"
        )
        completion_token = f"execution:{execution_id}:{values.get('final_message_id') or '-'}"
        emit = svc.should_emit_completion_signal(
            str(session_id), completion_token, acked_completion_token
        )
        return {
            "is_streaming": False,
            "just_completed": bool(emit and final_ready),
            "completion_token": completion_token if emit and final_ready else None,
            **svc.stream_status_payload("completed" if final_ready else "finalizing"),
            "content_length": len(partial),
            "partial_content": partial or None,
            "tool_count": tool_count,
            "last_tool": last_tool,
            "execution_id": execution_id,
            "last_event_id": values.get("last_event_id"),
            "final_message_id": values.get("final_message_id"),
            "final_message_ready": final_ready,
            "repair_required": not final_ready,
        }
    if phase == "interrupted":
        return {
            "is_streaming": False,
            "just_completed": False,
            **svc.stream_status_payload("needs_continuation"),
            "content_length": len(partial),
            "partial_content": partial or None,
            "tool_count": tool_count,
            "last_tool": last_tool,
            "execution_id": execution_id,
            "last_event_id": values.get("last_event_id"),
            "final_message_id": values.get("final_message_id"),
            "final_message_ready": False,
        }
    standalone_placeholder_id = values.get("placeholder_message_id")
    if standalone_placeholder_id and not execution_id:
        producer_live = bool(has_live_runtime)
        return {
            "is_streaming": producer_live,
            "just_completed": False,
            **svc.stream_status_payload(
                "generating" if producer_live else "needs_continuation"
            ),
            "content_length": len(partial),
            "partial_content": partial or None,
            "tool_count": tool_count,
            "last_tool": last_tool,
            "execution_id": values.get("standalone_placeholder_execution_id"),
            "placeholder_message_id": standalone_placeholder_id,
            "placeholder_ready": True,
            "repair_required": not producer_live,
        }
    recovered_id = values.get("recovered_message_id")
    if recovered_id:
        completion_token = f"recovered:{recovered_id}"
        emit = svc.should_emit_completion_signal(
            str(session_id), completion_token, acked_completion_token
        )
        return {
            "is_streaming": False,
            "just_completed": emit,
            "recovered": True,
            "completion_token": completion_token if emit else None,
            **svc.stream_status_payload("completed"),
            "final_message_id": recovered_id,
            "final_message_ready": True,
        }
    interrupted_execution_id = values.get("interrupted_execution_id")
    if interrupted_execution_id:
        interrupted_partial = str(values.get("interrupted_partial_content") or "")
        return {
            "is_streaming": False,
            "just_completed": False,
            **svc.stream_status_payload("needs_continuation"),
            "content_length": len(interrupted_partial),
            "partial_content": interrupted_partial or None,
            "execution_id": interrupted_execution_id,
            "last_event_id": values.get("interrupted_last_event_id"),
            "repair_required": False,
        }
    return dict(cached_status or {"is_streaming": False, **svc.stream_status_payload("completed")})


async def get_last_response_projection(
    *,
    session_id: UUID,
    tenant_id: UUID,
    has_live_runtime: bool,
) -> dict[str, Any]:
    """Project recovery state for the legacy last-response shape without DML."""
    from app.services import chat_service as svc

    async with _pool().acquire() as conn:
        async with _read_only_transaction(conn):
            state = await conn.fetchrow(
                """
                SELECT s.id,
                       te.id::text AS execution_id,
                       te.status,
                       te.last_event_id,
                       (te.owner_instance IS NOT NULL AND te.lease_expires_at > NOW()) AS lease_valid,
                       COALESCE(pm.id, am.id)::text AS message_id,
                       COALESCE(pm.content, am.content, '') AS partial_content,
                       COALESCE(am.model_used, pm.model_used, 'interrupted') AS model_used,
                       COALESCE(am.intent, pm.intent) AS intent,
                       COALESCE(am.created_at, pm.created_at) AS message_created_at
                FROM chat_sessions s
                LEFT JOIN LATERAL (
                    SELECT candidate.*
                    FROM chat_turn_executions candidate
                    WHERE candidate.session_id = s.id
                    ORDER BY
                        CASE WHEN candidate.status IN ('running', 'retrying') THEN 0 ELSE 1 END,
                        candidate.updated_at DESC,
                        candidate.id DESC
                    LIMIT 1
                ) te ON TRUE
                LEFT JOIN chat_messages am ON am.id = te.assistant_message_id
                LEFT JOIN LATERAL (
                    SELECT candidate.id, candidate.content, candidate.model_used,
                           candidate.intent, candidate.created_at
                    FROM chat_messages candidate
                    WHERE candidate.execution_id = te.id
                      AND candidate.intent = 'streaming_placeholder'
                    ORDER BY candidate.created_at DESC, candidate.id DESC
                    LIMIT 1
                ) pm ON TRUE
                WHERE s.id = $1 AND s.tenant_id = $2
                """,
                session_id,
                tenant_id,
            )
            if not state:
                raise ChatReadModelError(
                    "chat_session_not_found", "chat session not found", status_code=404
                )
            state_values = dict(state)
            phase = str(state_values.get("status") or "")
            partial = svc._strip_streaming_progress_markers(
                state_values.get("partial_content") or ""
            ).strip()
            if phase in {"running", "retrying"} and (
                has_live_runtime or state_values.get("lease_valid")
            ):
                return {"found": False, "generating": True}
            if phase in {"running", "retrying", "interrupted"} and partial:
                return {
                    "found": True,
                    "generating": False,
                    "repair_required": phase in {"running", "retrying"},
                    "message": {
                        "id": state_values.get("message_id")
                        or state_values.get("placeholder_id"),
                        "session_id": str(session_id),
                        "role": "assistant",
                        "content": partial,
                        "model_used": state_values.get("model_used") or "interrupted",
                        "created_at": state_values.get("message_created_at"),
                        "intent": "interrupted_partial"
                        if phase in {"running", "retrying"}
                        else state_values.get("intent"),
                        "execution_id": state_values.get("execution_id"),
                    },
                }
            latest_user = await conn.fetchrow(
                "SELECT id, created_at FROM chat_messages "
                "WHERE session_id = $1 AND tenant_id = $2 AND role = 'user' "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                session_id,
                tenant_id,
            )
            latest = await conn.fetchrow(
                """
                SELECT m.id::text AS message_id, m.content, m.model_used,
                       m.intent, m.execution_id::text AS execution_id,
                       m.created_at
                FROM chat_messages m
                LEFT JOIN chat_turn_executions te ON te.id = m.execution_id
                WHERE m.session_id = $1
                  AND m.tenant_id = $2
                  AND m.role = 'assistant'
                  AND m.intent IS DISTINCT FROM 'streaming_placeholder'
                  AND COALESCE(m.intent, '') NOT IN ('auto_reaction', 'system_trigger')
                  AND (
                    $3::uuid IS NULL
                    OR te.user_message_id = $3::uuid
                    OR m.created_at > $4::timestamptz
                  )
                ORDER BY m.created_at DESC, m.id DESC
                LIMIT 1
                """,
                session_id,
                tenant_id,
                latest_user["id"] if latest_user else None,
                latest_user["created_at"] if latest_user else None,
            )
    if not latest:
        return {"found": False, "generating": False}
    if latest_user and latest["created_at"] < latest_user["created_at"]:
        return {"found": False, "generating": False}
    return {
        "found": True,
        "generating": False,
        "message": {
            "id": latest["message_id"],
            "session_id": str(session_id),
            "role": "assistant",
            "content": latest["content"],
            "model_used": latest["model_used"],
            "created_at": latest["created_at"],
            "intent": latest["intent"],
            "execution_id": latest["execution_id"],
        },
    }
