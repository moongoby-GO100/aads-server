"""Chat data cleanup helpers.

The cleanup is intentionally narrow: only rows that are already hidden from the
chat UI via intent='_deleted_duplicate' are eligible for physical deletion.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)


def _bounded_int(value: Optional[int | str], *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


async def cleanup_deleted_duplicate_messages(
    *,
    retention_days: Optional[int | str] = None,
    batch_size: Optional[int | str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Delete old soft-deleted duplicate chat messages in bounded batches."""
    days = _bounded_int(retention_days, default=7, minimum=1, maximum=365)
    limit = _bounded_int(batch_size, default=1000, minimum=1, maximum=10000)

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, session_id
            FROM chat_messages
            WHERE intent = '_deleted_duplicate'
              AND COALESCE(edited_at, created_at) < NOW() - ($1::int * INTERVAL '1 day')
            ORDER BY COALESCE(edited_at, created_at) ASC
            LIMIT $2
            """,
            days,
            limit,
        )
        ids = [row["id"] for row in rows]
        session_ids = sorted({str(row["session_id"]) for row in rows})

        eligible_count = await conn.fetchval(
            """
            SELECT count(*)
            FROM chat_messages
            WHERE intent = '_deleted_duplicate'
              AND COALESCE(edited_at, created_at) < NOW() - ($1::int * INTERVAL '1 day')
            """,
            days,
        )

        if dry_run or not ids:
            return {
                "dry_run": dry_run,
                "eligible": int(eligible_count or 0),
                "deleted": 0,
                "sessions_touched": len(session_ids),
                "retention_days": days,
                "batch_size": limit,
            }

        deleted_rows = await conn.fetch(
            """
            DELETE FROM chat_messages
            WHERE id = ANY($1::uuid[])
            RETURNING session_id
            """,
            ids,
        )
        touched_sessions = sorted({str(row["session_id"]) for row in deleted_rows})
        for session_id in touched_sessions:
            session_uuid = uuid.UUID(session_id)
            await conn.execute(
                """
                UPDATE chat_sessions
                SET message_count = (
                        SELECT count(*)
                        FROM chat_messages
                        WHERE session_id = $1
                    ),
                    updated_at = NOW()
                WHERE id = $1
                """,
                session_uuid,
            )

        result = {
            "dry_run": False,
            "eligible": int(eligible_count or 0),
            "deleted": len(deleted_rows),
            "sessions_touched": len(touched_sessions),
            "retention_days": days,
            "batch_size": limit,
        }
        if result["deleted"]:
            logger.info("chat_deleted_duplicate_cleanup", extra=result)
        return result
