"""WP05 durable chat command lifecycle, idempotency, and generation fencing.

Three invariants hold this module together:

* **Durable before side effect.**  A command row commits before the work it
  authorises begins, so a process restart or a blue/green slot switch can always
  recover the command's final state instead of leaving the caller guessing.
* **Idempotent by key, fail-closed on reuse.**  The same
  (tenant, session, type, idempotency_key) resolves to the same command.  Reusing
  a key with a *different* request body is a client bug and is rejected, never
  answered with someone else's result.
* **Fenced by owner_epoch.**  Generation identity is keyed by
  ``(execution_id, owner_epoch)``, which is exactly WP03's fence and WP04's
  checkpoint scope.  This module does not invent a second ownership contract.

Nothing here is wired into the v2 production flag; WP03 continues to advertise
``production_ready: False`` until the operational gates are opened separately.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog

logger = structlog.get_logger(__name__)

COMMAND_TYPES = frozenset({"send", "interrupt", "resume", "retry", "stop"})
COMMAND_STATUSES = frozenset(
    {"accepted", "running", "succeeded", "failed", "superseded"}
)
TERMINAL_COMMAND_STATUSES = frozenset({"succeeded", "failed", "superseded"})
GENERATION_STATUSES = frozenset({"active", "superseded", "completed", "failed"})

MAX_IDEMPOTENCY_KEY_LENGTH = 200
# A command that never reported back within this window is recovered rather than
# left in-flight forever.  It is deliberately longer than the execution lease so
# a merely slow generation is not stolen from its live owner.
DEFAULT_ORPHAN_GRACE_SECONDS = 900


class ChatCommandError(RuntimeError):
    """Stable, client-visible WP05 command contract failure."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _pool():
    # Lazy import keeps the pool seam identical to chat_read_model/chat_repair.
    from app.core.db_pool import get_pool

    return get_pool()


@dataclass(frozen=True)
class ChatCommandRecord:
    """One durable command row, normalised for transport."""

    command_id: UUID
    tenant_id: UUID
    session_id: UUID
    command_type: str
    idempotency_key: str
    request_fingerprint: str
    status: str
    owner_epoch: str
    attempt: int
    execution_id: UUID | None = None
    generation_id: UUID | None = None
    owner_instance: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_COMMAND_STATUSES

    def to_payload(self, *, replayed: bool = False) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "contract_version": 2,
            "command_id": str(self.command_id),
            "session_id": str(self.session_id),
            "command_type": self.command_type,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "terminal": self.is_terminal,
            "replayed": replayed,
            "execution_id": str(self.execution_id) if self.execution_id else None,
            "generation_id": str(self.generation_id) if self.generation_id else None,
            "owner_epoch": self.owner_epoch,
            "attempt": self.attempt,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class ChatGenerationRecord:
    """Stable identity for one uninterrupted attempt at an execution."""

    generation_id: UUID
    execution_id: UUID
    session_id: UUID
    owner_epoch: str
    attempt: int
    status: str
    owner_instance: str | None = None
    command_id: UUID | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    superseded_by_epoch: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "contract_version": 2,
            "generation_id": str(self.generation_id),
            "execution_id": str(self.execution_id),
            "session_id": str(self.session_id),
            "owner_epoch": self.owner_epoch,
            "attempt": self.attempt,
            "status": self.status,
            "owner_instance": self.owner_instance,
            "command_id": str(self.command_id) if self.command_id else None,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "superseded_by_epoch": self.superseded_by_epoch,
        }


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def command_fingerprint(payload: Any) -> str:
    """Hash the semantic request body so key reuse with a new body is detectable."""
    canonical = json.dumps(
        _json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_command_type(command_type: str) -> str:
    value = str(command_type or "").strip().lower()
    if value not in COMMAND_TYPES:
        raise ChatCommandError(
            "unsupported_chat_command_type",
            f"chat command type must be one of {sorted(COMMAND_TYPES)}",
            status_code=400,
        )
    return value


def normalize_idempotency_key(idempotency_key: str | None) -> str:
    value = str(idempotency_key or "").strip()
    if not value:
        raise ChatCommandError(
            "missing_chat_idempotency_key",
            "a non-empty Idempotency-Key is required for durable chat commands",
            status_code=400,
        )
    if len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ChatCommandError(
            "invalid_chat_idempotency_key",
            f"Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters",
            status_code=400,
        )
    return value


def _as_json_object(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            # A stored payload we cannot parse is a schema mismatch, not an
            # empty result: surface it instead of silently dropping it.
            raise ChatCommandError(
                "chat_command_payload_unreadable",
                "stored chat command payload is not valid JSON",
                status_code=500,
            ) from None
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return {"value": _json_value(value)}


def _optional_uuid(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    return UUID(str(value))


def _command_from_row(row: Any) -> ChatCommandRecord:
    values = dict(row)
    status = str(values.get("status") or "")
    if status not in COMMAND_STATUSES:
        raise ChatCommandError(
            "chat_command_status_unknown",
            f"stored chat command status {status!r} is not part of the WP05 contract",
            status_code=500,
        )
    return ChatCommandRecord(
        command_id=UUID(str(values["command_id"])),
        tenant_id=UUID(str(values["tenant_id"])),
        session_id=UUID(str(values["session_id"])),
        command_type=str(values["command_type"]),
        idempotency_key=str(values["idempotency_key"]),
        request_fingerprint=str(values["request_fingerprint"]),
        status=status,
        owner_epoch=str(int(values.get("owner_epoch") or 0)),
        attempt=int(values.get("attempt") or 0),
        execution_id=_optional_uuid(values.get("execution_id")),
        generation_id=_optional_uuid(values.get("generation_id")),
        owner_instance=values.get("owner_instance"),
        result=_as_json_object(values.get("result")),
        error=_as_json_object(values.get("error")),
        created_at=values.get("created_at"),
        updated_at=values.get("updated_at"),
        completed_at=values.get("completed_at"),
    )


def _generation_from_row(row: Any) -> ChatGenerationRecord:
    values = dict(row)
    status = str(values.get("status") or "")
    if status not in GENERATION_STATUSES:
        raise ChatCommandError(
            "chat_generation_status_unknown",
            f"stored generation status {status!r} is not part of the WP05 contract",
            status_code=500,
        )
    max_epoch = values.get("max_owner_epoch")
    owner_epoch = int(values.get("owner_epoch") or 0)
    superseded_by = (
        str(int(max_epoch))
        if max_epoch is not None and int(max_epoch) > owner_epoch
        else None
    )
    return ChatGenerationRecord(
        generation_id=UUID(str(values["generation_id"])),
        execution_id=UUID(str(values["execution_id"])),
        session_id=UUID(str(values["session_id"])),
        owner_epoch=str(owner_epoch),
        attempt=int(values.get("attempt") or 1),
        status=status,
        owner_instance=values.get("owner_instance"),
        command_id=_optional_uuid(values.get("command_id")),
        started_at=values.get("started_at"),
        ended_at=values.get("ended_at"),
        superseded_by_epoch=superseded_by,
    )


async def schema_ready(conn: Any) -> bool:
    """Report whether migration 176 is present, without mutating anything."""
    row = await conn.fetchrow(
        """
        SELECT to_regclass('chat_commands') IS NOT NULL AS commands,
               to_regclass('chat_execution_generations') IS NOT NULL AS generations,
               EXISTS (
                   SELECT 1
                   FROM information_schema.columns
                   WHERE table_schema = current_schema()
                     AND table_name = 'chat_turn_executions'
                     AND column_name = 'generation_id'
               ) AS execution_generation_id
        """
    )
    return bool(
        row
        and row["commands"]
        and row["generations"]
        and row["execution_generation_id"]
    )


async def _require_schema(conn: Any) -> None:
    if not await schema_ready(conn):
        raise ChatCommandError(
            "chat_command_migration_required",
            "the additive WP05 chat command lifecycle migration is not available",
            status_code=503,
        )


def wp05_migration_ready() -> bool:
    """Operational gate for advertising WP05 as available.

    Capability discovery is synchronous, so this mirrors the WP04 gate and reads
    an operator-set environment flag rather than probing the database inline.
    """
    return os.getenv("AADS_CHAT_WP05_MIGRATION_READY", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def wp05_command_lifecycle_ready() -> bool:
    """Async readiness probe against the live schema; never raises."""
    try:
        async with _pool().acquire() as conn:
            return await schema_ready(conn)
    except Exception:  # noqa: BLE001  # pragma: no cover - probe must not break discovery
        return False


# ── Lifecycle ───────────────────────────────────────────────────────────────


async def begin_command(
    *,
    tenant_id: UUID,
    session_id: UUID,
    command_type: str,
    idempotency_key: str,
    payload: Any = None,
    execution_id: UUID | None = None,
) -> tuple[ChatCommandRecord, bool]:
    """Durably accept a command, or replay the one this key already created.

    Returns ``(record, replayed)``.  When ``replayed`` is true the caller must
    not run the side effect again: either the original command is still in
    flight, or its stored result is authoritative.
    """
    normalized_type = normalize_command_type(command_type)
    key = normalize_idempotency_key(idempotency_key)
    fingerprint = command_fingerprint(payload)

    async with _pool().acquire() as conn:
        await _require_schema(conn)
        async with conn.transaction():
            inserted = await conn.fetchrow(
                """
                INSERT INTO chat_commands (
                    tenant_id, session_id, command_type, idempotency_key,
                    request_fingerprint, status, execution_id
                )
                SELECT $1, $2, $3, $4, $5, 'accepted', $6
                WHERE EXISTS (
                    SELECT 1 FROM chat_sessions
                    WHERE id = $2 AND tenant_id = $1
                )
                ON CONFLICT ON CONSTRAINT uq_chat_commands_idempotency DO NOTHING
                RETURNING *
                """,
                tenant_id,
                session_id,
                normalized_type,
                key,
                fingerprint,
                execution_id,
            )
            if inserted is not None:
                record = _command_from_row(inserted)
                logger.info(
                    "chat_command_accepted",
                    tenant_id=str(tenant_id),
                    session_id=str(session_id),
                    command_id=str(record.command_id),
                    command_type=normalized_type,
                )
                return record, False

            existing = await conn.fetchrow(
                """
                SELECT *
                FROM chat_commands
                WHERE tenant_id = $1
                  AND session_id = $2
                  AND command_type = $3
                  AND idempotency_key = $4
                FOR UPDATE
                """,
                tenant_id,
                session_id,
                normalized_type,
                key,
            )
            if existing is None:
                # No conflicting row means the guarded INSERT found no session
                # in this tenant.  Fail closed rather than creating one.
                raise ChatCommandError(
                    "chat_command_session_not_found",
                    "chat session not found for this tenant",
                    status_code=404,
                )
            record = _command_from_row(existing)
            if record.request_fingerprint != fingerprint:
                raise ChatCommandError(
                    "chat_idempotency_key_reuse_conflict",
                    "this Idempotency-Key was already used for a different request body",
                    status_code=409,
                )
            logger.info(
                "chat_command_replayed",
                tenant_id=str(tenant_id),
                session_id=str(session_id),
                command_id=str(record.command_id),
                status=record.status,
            )
            return record, True


async def mark_command_running(
    *,
    command_id: UUID,
    tenant_id: UUID,
    owner_epoch: int | str = 0,
    execution_id: UUID | None = None,
    generation_id: UUID | None = None,
    owner_instance: str | None = None,
) -> ChatCommandRecord:
    """Move an accepted command to running under a fence, bumping its attempt."""
    epoch = int(owner_epoch)
    resolved_instance = owner_instance
    if resolved_instance is None:
        from app.services import chat_service as svc

        resolved_instance = svc._EXECUTION_OWNER_INSTANCE

    async with _pool().acquire() as conn:
        await _require_schema(conn)
        row = await conn.fetchrow(
            """
            UPDATE chat_commands
            SET status = 'running',
                attempt = attempt + 1,
                owner_epoch = $3,
                owner_instance = $4,
                execution_id = COALESCE($5, execution_id),
                generation_id = COALESCE($6, generation_id)
            WHERE command_id = $1
              AND tenant_id = $2
              AND status IN ('accepted', 'running')
              AND owner_epoch <= $3
            RETURNING *
            """,
            command_id,
            tenant_id,
            epoch,
            resolved_instance,
            execution_id,
            generation_id,
        )
        if row is None:
            await _raise_transition_failure(
                conn,
                command_id=command_id,
                tenant_id=tenant_id,
                owner_epoch=epoch,
                target="running",
            )
    return _command_from_row(row)


async def complete_command(
    *,
    command_id: UUID,
    tenant_id: UUID,
    result: dict[str, Any] | None = None,
    owner_epoch: int | str | None = None,
    execution_id: UUID | None = None,
    generation_id: UUID | None = None,
) -> ChatCommandRecord:
    """Record the authoritative success result exactly once."""
    return await _settle_command(
        command_id=command_id,
        tenant_id=tenant_id,
        status="succeeded",
        result=result,
        error=None,
        owner_epoch=owner_epoch,
        execution_id=execution_id,
        generation_id=generation_id,
    )


async def fail_command(
    *,
    command_id: UUID,
    tenant_id: UUID,
    code: str,
    message: str,
    owner_epoch: int | str | None = None,
    execution_id: UUID | None = None,
    generation_id: UUID | None = None,
) -> ChatCommandRecord:
    """Record a terminal failure so the client stops polling an in-flight state."""
    return await _settle_command(
        command_id=command_id,
        tenant_id=tenant_id,
        status="failed",
        result=None,
        error={"code": str(code), "message": str(message)[:500]},
        owner_epoch=owner_epoch,
        execution_id=execution_id,
        generation_id=generation_id,
    )


async def supersede_command(
    *,
    command_id: UUID,
    tenant_id: UUID,
    reason: str,
    owner_epoch: int | str | None = None,
) -> ChatCommandRecord:
    """Terminally retire a command that a newer generation has taken over."""
    return await _settle_command(
        command_id=command_id,
        tenant_id=tenant_id,
        status="superseded",
        result=None,
        error={"code": "chat_command_superseded", "message": str(reason)[:500]},
        owner_epoch=owner_epoch,
        execution_id=None,
        generation_id=None,
    )


async def _settle_command(
    *,
    command_id: UUID,
    tenant_id: UUID,
    status: str,
    result: dict[str, Any] | None,
    error: dict[str, Any] | None,
    owner_epoch: int | str | None,
    execution_id: UUID | None,
    generation_id: UUID | None,
) -> ChatCommandRecord:
    epoch = None if owner_epoch is None else int(owner_epoch)
    async with _pool().acquire() as conn:
        await _require_schema(conn)
        row = await conn.fetchrow(
            """
            UPDATE chat_commands
            SET status = $3,
                result = $4::jsonb,
                error = $5::jsonb,
                owner_epoch = COALESCE($6, owner_epoch),
                execution_id = COALESCE($7, execution_id),
                generation_id = COALESCE($8, generation_id),
                completed_at = NOW()
            WHERE command_id = $1
              AND tenant_id = $2
              AND status IN ('accepted', 'running')
              AND ($6::bigint IS NULL OR owner_epoch <= $6)
            RETURNING *
            """,
            command_id,
            tenant_id,
            status,
            json.dumps(_json_value(result), ensure_ascii=False) if result is not None else None,
            json.dumps(_json_value(error), ensure_ascii=False) if error is not None else None,
            epoch,
            execution_id,
            generation_id,
        )
        if row is None:
            await _raise_transition_failure(
                conn,
                command_id=command_id,
                tenant_id=tenant_id,
                owner_epoch=epoch,
                target=status,
            )
    return _command_from_row(row)


async def _raise_transition_failure(
    conn: Any,
    *,
    command_id: UUID,
    tenant_id: UUID,
    owner_epoch: int | None,
    target: str,
) -> None:
    """Translate a refused fenced UPDATE into a specific, fail-closed error."""
    current = await conn.fetchrow(
        "SELECT * FROM chat_commands WHERE command_id = $1 AND tenant_id = $2",
        command_id,
        tenant_id,
    )
    if current is None:
        raise ChatCommandError(
            "chat_command_not_found",
            "chat command not found for this tenant",
            status_code=404,
        )
    record = _command_from_row(current)
    if record.is_terminal:
        raise ChatCommandError(
            "chat_command_already_terminal",
            f"chat command is already {record.status} and cannot become {target}",
            status_code=409,
        )
    raise ChatCommandError(
        "chat_command_fenced",
        "a newer owner epoch holds this chat command; the stale writer must exit",
        status_code=409,
    )


async def get_command(
    *,
    command_id: UUID,
    tenant_id: UUID,
    session_id: UUID | None = None,
) -> ChatCommandRecord:
    """Read one command's durable state.  Read-only: never repairs or settles."""
    async with _pool().acquire() as conn:
        await _require_schema(conn)
        row = await conn.fetchrow(
            """
            SELECT *
            FROM chat_commands
            WHERE command_id = $1
              AND tenant_id = $2
              AND ($3::uuid IS NULL OR session_id = $3::uuid)
            """,
            command_id,
            tenant_id,
            session_id,
        )
    if row is None:
        raise ChatCommandError(
            "chat_command_not_found",
            "chat command not found for this tenant",
            status_code=404,
        )
    return _command_from_row(row)


# ── Generation identity ─────────────────────────────────────────────────────


async def resolve_generation(
    *,
    execution_id: UUID,
    tenant_id: UUID,
    owner_epoch: int | str | None = None,
) -> ChatGenerationRecord:
    """Resolve the stable generation for an execution, fencing stale epochs.

    With no ``owner_epoch`` this returns the newest generation.  With one, it
    returns that exact generation and fails closed if a newer epoch has already
    superseded it, so a stale writer learns it has lost the fence.
    """
    epoch = None if owner_epoch is None else int(owner_epoch)
    async with _pool().acquire() as conn:
        await _require_schema(conn)
        row = await conn.fetchrow(
            """
            SELECT g.*,
                   (
                       SELECT MAX(peer.owner_epoch)
                       FROM chat_execution_generations peer
                       WHERE peer.execution_id = g.execution_id
                   ) AS max_owner_epoch
            FROM chat_execution_generations g
            JOIN chat_sessions s
              ON s.id = g.session_id AND s.tenant_id = g.tenant_id
            WHERE g.execution_id = $1
              AND g.tenant_id = $2
              AND ($3::bigint IS NULL OR g.owner_epoch = $3::bigint)
            ORDER BY g.owner_epoch DESC
            LIMIT 1
            """,
            execution_id,
            tenant_id,
            epoch,
        )
    if row is None:
        raise ChatCommandError(
            "chat_generation_not_found",
            "no generation is recorded for this execution and epoch",
            status_code=404,
        )
    record = _generation_from_row(row)
    if epoch is not None and record.superseded_by_epoch is not None:
        raise ChatCommandError(
            "chat_generation_fenced",
            (
                f"generation epoch {record.owner_epoch} was superseded by epoch "
                f"{record.superseded_by_epoch}"
            ),
            status_code=409,
        )
    return record


async def settle_generation(
    *,
    execution_id: UUID,
    tenant_id: UUID,
    owner_epoch: int | str,
    status: str,
) -> ChatGenerationRecord:
    """Close one generation under its own fence; a superseded one cannot settle."""
    if status not in {"completed", "failed"}:
        raise ChatCommandError(
            "unsupported_chat_generation_status",
            "a generation may only settle as completed or failed",
            status_code=400,
        )
    epoch = int(owner_epoch)
    async with _pool().acquire() as conn:
        await _require_schema(conn)
        row = await conn.fetchrow(
            """
            UPDATE chat_execution_generations g
            SET status = $4,
                ended_at = NOW()
            WHERE g.execution_id = $1
              AND g.tenant_id = $2
              AND g.owner_epoch = $3
              AND g.status = 'active'
              AND NOT EXISTS (
                  SELECT 1
                  FROM chat_execution_generations peer
                  WHERE peer.execution_id = g.execution_id
                    AND peer.owner_epoch > g.owner_epoch
              )
            RETURNING g.*, g.owner_epoch AS max_owner_epoch
            """,
            execution_id,
            tenant_id,
            epoch,
            status,
        )
        if row is None:
            # Distinguish "gone" from "fenced out" from "already settled".
            await _raise_generation_settle_failure(
                conn,
                execution_id=execution_id,
                tenant_id=tenant_id,
                owner_epoch=epoch,
            )
    return _generation_from_row(row)


async def _raise_generation_settle_failure(
    conn: Any,
    *,
    execution_id: UUID,
    tenant_id: UUID,
    owner_epoch: int,
) -> None:
    """Always raises: explain exactly why a fenced generation settle was refused."""
    row = await conn.fetchrow(
        """
        SELECT g.*,
               (
                   SELECT MAX(peer.owner_epoch)
                   FROM chat_execution_generations peer
                   WHERE peer.execution_id = g.execution_id
               ) AS max_owner_epoch
        FROM chat_execution_generations g
        WHERE g.execution_id = $1 AND g.tenant_id = $2 AND g.owner_epoch = $3
        """,
        execution_id,
        tenant_id,
        owner_epoch,
    )
    if row is None:
        raise ChatCommandError(
            "chat_generation_not_found",
            "no generation is recorded for this execution and epoch",
            status_code=404,
        )
    values = dict(row)
    max_epoch = values.get("max_owner_epoch")
    if max_epoch is not None and int(max_epoch) > owner_epoch:
        raise ChatCommandError(
            "chat_generation_fenced",
            (
                f"generation epoch {owner_epoch} was superseded by epoch "
                f"{int(max_epoch)}"
            ),
            status_code=409,
        )
    raise ChatCommandError(
        "chat_generation_already_settled",
        f"generation epoch {owner_epoch} is already {values.get('status')}",
        status_code=409,
    )


# ── Restart / slot-switch recovery ──────────────────────────────────────────


async def recover_orphaned_commands(
    *,
    limit: int = 50,
    grace_seconds: int = DEFAULT_ORPHAN_GRACE_SECONDS,
) -> dict[str, int]:
    """Settle commands whose owner vanished, so no command is in flight forever.

    A command is orphaned when it is still ``accepted``/``running`` past the
    grace window and its execution is either absent or already terminal.  The
    outcome mirrors the execution: a completed execution settles the command as
    succeeded, anything else fails closed.  Live executions are never touched.
    """
    bounded_limit = max(1, min(int(limit), 500))
    bounded_grace = max(60, int(grace_seconds))
    from app.services import chat_service as svc

    if not svc._is_local_active_api_slot():
        return {
            "scanned": 0,
            "recovered_succeeded": 0,
            "recovered_failed": 0,
            "skipped": 0,
            "inactive_slot_skipped": 1,
        }

    async with _pool().acquire() as conn:
        if not await schema_ready(conn):
            return {
                "scanned": 0,
                "recovered_succeeded": 0,
                "recovered_failed": 0,
                "skipped": 0,
                "schema_unavailable": 1,
            }
        candidates = await conn.fetch(
            """
            SELECT c.command_id,
                   c.tenant_id,
                   c.session_id,
                   c.owner_epoch,
                   te.id AS execution_id,
                   te.status AS execution_status
            FROM chat_commands c
            LEFT JOIN chat_turn_executions te ON te.id = c.execution_id
            WHERE c.status IN ('accepted', 'running')
              AND c.updated_at <= NOW() - ($2::int * INTERVAL '1 second')
              AND (
                  te.id IS NULL
                  OR te.status NOT IN ('running', 'retrying')
                  OR te.lease_expires_at IS NULL
                  OR te.lease_expires_at <= NOW()
              )
            ORDER BY c.updated_at ASC
            LIMIT $1
            """,
            bounded_limit,
            bounded_grace,
        )

    succeeded = 0
    failed = 0
    skipped = 0
    for row in candidates:
        execution_status = str(row["execution_status"] or "")
        try:
            if execution_status == "completed":
                await complete_command(
                    command_id=UUID(str(row["command_id"])),
                    tenant_id=UUID(str(row["tenant_id"])),
                    result={
                        "recovered": True,
                        "reason": "execution_completed_after_owner_loss",
                        "execution_status": execution_status,
                    },
                    owner_epoch=int(row["owner_epoch"] or 0),
                )
                succeeded += 1
            else:
                await fail_command(
                    command_id=UUID(str(row["command_id"])),
                    tenant_id=UUID(str(row["tenant_id"])),
                    code="chat_command_owner_lost",
                    message=(
                        "the owning process released this command without a terminal "
                        f"result (execution_status={execution_status or 'missing'})"
                    ),
                    owner_epoch=int(row["owner_epoch"] or 0),
                )
                failed += 1
        except ChatCommandError as exc:
            skipped += 1
            logger.info(
                "chat_command_recovery_skipped",
                command_id=str(row["command_id"]),
                reason=exc.code,
            )
    if candidates:
        logger.info(
            "chat_command_recovery_completed",
            scanned=len(candidates),
            recovered_succeeded=succeeded,
            recovered_failed=failed,
            skipped=skipped,
        )
    return {
        "scanned": len(candidates),
        "recovered_succeeded": succeeded,
        "recovered_failed": failed,
        "skipped": skipped,
        "inactive_slot_skipped": 0,
    }
