"""Explicit, DB-fenced writers for WP04 chat projection repair.

Reads never import or invoke this module.  A repair first claims a terminal
execution by advancing its owner epoch, holds the execution row lock for the
transaction, and releases only the lease for the exact owner/epoch it claimed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog

from app.core.db_pool import get_pool
from app.services import chat_service as svc


logger = structlog.get_logger(__name__)


class ChatRepairError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _stale_projection(message: dict[str, Any]) -> bool:
    return bool(
        message.get("role") == "assistant"
        and (
            str(message.get("intent") or "")
            in {
                "streaming_placeholder",
                "interrupted_partial",
                "interruption_notice",
                "_archived_partial",
            }
            or str(message.get("model_used") or "")
            in {"streaming", "interrupted", "stopped"}
        )
    )


async def repair_completed_execution_projection(
    *,
    session_id: UUID,
    execution_id: UUID,
    tenant_id: UUID,
    reason: str = "explicit_projection_repair",
) -> dict[str, Any]:
    """Repair one completed execution under a newly claimed terminal fence."""
    if not svc._is_local_active_api_slot():
        raise ChatRepairError(
            "inactive_chat_repair_writer",
            "the inactive API slot cannot claim chat repairs",
            status_code=503,
        )
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            claim = await conn.fetchrow(
                """
                UPDATE chat_turn_executions te
                SET owner_instance = $4,
                    owner_epoch = te.owner_epoch + 1,
                    heartbeat_at = NOW(),
                    lease_expires_at = NOW() + ($5::int * INTERVAL '1 second'),
                    updated_at = NOW()
                FROM chat_sessions s
                WHERE te.id = $1
                  AND te.session_id = $2
                  AND s.id = te.session_id
                  AND s.tenant_id = $3
                  AND te.status = 'completed'
                  AND te.completed_at IS NOT NULL
                  AND (
                    te.owner_instance IS NULL
                    OR te.lease_expires_at IS NULL
                    OR te.lease_expires_at <= NOW()
                    OR te.owner_instance = $4
                  )
                RETURNING te.owner_epoch
                """,
                execution_id,
                session_id,
                tenant_id,
                svc._EXECUTION_OWNER_INSTANCE,
                svc._EXECUTION_LEASE_SECONDS,
            )
            if not claim:
                exists = await conn.fetchval(
                    """
                    SELECT 1
                    FROM chat_turn_executions te
                    JOIN chat_sessions s ON s.id = te.session_id
                    WHERE te.id = $1 AND te.session_id = $2 AND s.tenant_id = $3
                    """,
                    execution_id,
                    session_id,
                    tenant_id,
                )
                if not exists:
                    raise ChatRepairError(
                        "chat_execution_not_found",
                        "chat execution not found",
                        status_code=404,
                    )
                raise ChatRepairError(
                    "chat_repair_fence_unavailable",
                    "chat execution repair lease is owned by another writer",
                )
            owner_epoch = int(claim["owner_epoch"])
            rows = await conn.fetch(
                """
                SELECT m.*
                FROM chat_messages m
                JOIN chat_turn_executions te ON te.id = m.execution_id
                JOIN chat_sessions s ON s.id = te.session_id
                WHERE m.execution_id = $1
                  AND m.session_id = $2
                  AND m.tenant_id = $3
                  AND s.tenant_id = $3
                  AND te.status = 'completed'
                  AND te.owner_instance = $4
                  AND te.owner_epoch = $5
                  AND te.lease_expires_at > NOW()
                ORDER BY m.created_at ASC, m.id ASC
                FOR UPDATE OF m
                """,
                execution_id,
                session_id,
                tenant_id,
                svc._EXECUTION_OWNER_INSTANCE,
                owner_epoch,
            )
            messages = [svc._row_to_dict(row) for row in rows]
            stale_before_ids = {
                str(message.get("id"))
                for message in messages
                if _stale_projection(message)
            }
            repaired_messages = await svc._repair_completed_execution_message_flags(
                conn,
                messages,
                f"explicit_repair:{reason[:80]}",
                persist=True,
            )
            remaining_by_id = {
                str(message.get("id")): message for message in repaired_messages
            }
            archived = len(stale_before_ids.difference(remaining_by_id))
            repaired = sum(
                1
                for message_id in stale_before_ids.intersection(remaining_by_id)
                if not _stale_projection(remaining_by_id[message_id])
            )
            released = await conn.fetchval(
                """
                UPDATE chat_turn_executions
                SET heartbeat_at = NOW(),
                    lease_expires_at = NULL,
                    updated_at = NOW()
                WHERE id = $1
                  AND session_id = $2
                  AND status = 'completed'
                  AND owner_instance = $3
                  AND owner_epoch = $4
                RETURNING owner_epoch
                """,
                execution_id,
                session_id,
                svc._EXECUTION_OWNER_INSTANCE,
                owner_epoch,
            )
            if released is None:
                raise ChatRepairError(
                    "chat_repair_fence_lost",
                    "chat repair lost its owner epoch before completion",
                )
    logger.info(
        "chat_projection_repair_completed",
        tenant_id=str(tenant_id),
        session_id=str(session_id),
        execution_id=str(execution_id),
        owner_instance=svc._EXECUTION_OWNER_INSTANCE,
        owner_epoch=owner_epoch,
        reason=reason[:200],
        repaired=repaired,
        archived=archived,
    )
    return {
        "session_id": str(session_id),
        "execution_id": str(execution_id),
        "claimed_owner_epoch": str(owner_epoch),
        "repaired": repaired,
        "archived": archived,
        "status": "repaired" if repaired or archived else "noop",
    }


async def repair_completed_projection_batch(*, limit: int = 20) -> dict[str, int]:
    """Worker entry point; candidates are revalidated by the fenced writer."""
    if not svc._is_local_active_api_slot():
        return {"scanned": 0, "repaired": 0, "skipped": 0, "inactive_slot_skipped": 1}
    bounded_limit = max(1, min(int(limit), 100))
    async with get_pool().acquire() as conn:
        candidates = await conn.fetch(
            """
            SELECT DISTINCT te.session_id, te.id AS execution_id, s.tenant_id
            FROM chat_turn_executions te
            JOIN chat_sessions s ON s.id = te.session_id
            JOIN chat_messages m ON m.execution_id = te.id
            WHERE te.status = 'completed'
              AND te.completed_at IS NOT NULL
              AND (
                m.intent IN (
                    'streaming_placeholder', 'interrupted_partial',
                    'interruption_notice', '_archived_partial'
                )
                OR m.model_used IN ('streaming', 'interrupted', 'stopped')
              )
              AND (te.lease_expires_at IS NULL OR te.lease_expires_at <= NOW())
            ORDER BY te.id
            LIMIT $1
            """,
            bounded_limit,
        )
    repaired = 0
    skipped = 0
    for row in candidates:
        try:
            result = await repair_completed_execution_projection(
                session_id=UUID(str(row["session_id"])),
                execution_id=UUID(str(row["execution_id"])),
                tenant_id=UUID(str(row["tenant_id"])),
                reason="scheduled_projection_repair",
            )
            repaired += int(result["repaired"]) + int(result["archived"])
        except ChatRepairError as exc:
            skipped += 1
            logger.info(
                "chat_projection_repair_skipped",
                execution_id=str(row["execution_id"]),
                reason=exc.code,
            )
    return {
        "scanned": len(candidates),
        "repaired": repaired,
        "skipped": skipped,
        "inactive_slot_skipped": 0,
    }
