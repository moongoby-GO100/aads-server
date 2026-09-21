"""Deliver terminal deployment results to the chat session that registered them."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

from app.core.db_pool import get_pool
from app.services.session_reporter import post_session_report

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = (
    "completed",
    "success",
    "success_partial",
    "failed",
    "error",
    "blocked",
    "cancelled",
    "superseded",
)
ACTIONABLE_STATUSES = frozenset({"success_partial", "failed", "error", "blocked", "cancelled"})
MAX_ATTEMPTS = 3
CLAIM_STALE_SECONDS = 300


def _owner_instance() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _report_body(row: dict[str, Any]) -> str:
    error = str(row.get("error_summary") or "없음").strip()[:1500]
    return (
        f"배포 #{row['id']}가 종료됐습니다.\n"
        f"상태: {row.get('status') or 'unknown'}\n"
        f"단계: {row.get('phase') or 'unknown'}\n"
        f"대상: {row.get('project') or 'unknown'}/{row.get('component') or 'api'}\n"
        f"릴리스 SHA: {row.get('release_sha') or 'unknown'}\n"
        f"오류 요약: {error}"
    )


def _reaction_prompt(row: dict[str, Any]) -> str:
    return (
        "[시스템] 이 세션에서 등록한 배포가 실패하거나 완전 인증되지 않았습니다. "
        "deploy_runs와 phase event, 서비스 로그, health를 실제 조회해 원인을 진단하고, "
        "현재 승인 범위 안의 비파괴 조치는 수행한 뒤 CEO에게 결과를 보고하세요. "
        "재배포·재시작·삭제처럼 별도 승인이 필요한 조치는 승인 정책을 우회하지 마세요.\n\n"
        + _report_body(row)
    )


async def claim_pending_notifications(
    conn: Any,
    *,
    owner: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Claim terminal callbacks with DB row locks and a stale-claim fence."""
    rows = await conn.fetch(
        """
        WITH candidates AS (
            SELECT id, session_notification_status AS previous_notification_status
              FROM deploy_runs
             WHERE chat_session_id IS NOT NULL
               AND status = ANY($1::text[])
               AND (
                    session_notification_status IN ('pending', 'reported')
                    OR (
                        session_notification_status = 'processing'
                        AND session_notification_claimed_at < NOW() - ($2::int * INTERVAL '1 second')
                    )
               )
             ORDER BY COALESCE(phase_completed_at, updated_at, created_at), id
             LIMIT $3
             FOR UPDATE SKIP LOCKED
        )
        UPDATE deploy_runs AS dr
           SET session_notification_status = 'processing',
               session_notification_owner = $4,
               session_notification_claimed_at = NOW(),
               session_notification_attempts = session_notification_attempts + 1,
               session_notification_error = NULL
          FROM candidates AS c
         WHERE dr.id = c.id
        RETURNING dr.*, c.previous_notification_status
        """,
        list(TERMINAL_STATUSES),
        CLAIM_STALE_SECONDS,
        max(1, min(int(limit), 20)),
        owner[:160],
    )
    return [dict(row) for row in rows]


async def _finish(
    conn: Any,
    *,
    run_id: int,
    owner: str,
    status: str,
    error: str = "",
) -> bool:
    command = await conn.execute(
        """
        UPDATE deploy_runs
           SET session_notification_status = $3,
               session_notification_owner = NULL,
               session_notification_claimed_at = NULL,
               session_notified_at = CASE WHEN $3 = 'notified' THEN NOW() ELSE session_notified_at END,
               session_notification_error = NULLIF($4, '')
         WHERE id = $1
           AND session_notification_owner = $2
           AND session_notification_status = 'processing'
        """,
        int(run_id),
        owner[:160],
        status,
        error[:1000],
    )
    return command.endswith(" 1")


async def process_claimed_notification(row: dict[str, Any], *, owner: str) -> bool:
    """Persist one report and, for problem states, request an AI reaction."""
    pool = get_pool()
    previous = str(row.get("previous_notification_status") or "pending")
    run_id = int(row["id"])
    deploy_status = str(row.get("status") or "unknown").lower()
    reported = previous == "reported"

    if not reported:
        result = await post_session_report(
            session_id=row.get("chat_session_id"),
            title=f"{row.get('project') or 'AADS'} 배포 {deploy_status}",
            body=_report_body(row),
            status="warning" if deploy_status in ACTIONABLE_STATUSES else "success",
            source="deploy_session_callback",
            project=str(row.get("project") or "AADS"),
            metadata={"deploy_run_id": run_id, "release_sha": row.get("release_sha")},
            intent="deploy_terminal_report",
            idempotency_key=f"deploy-run:{run_id}:terminal:{deploy_status}",
            trigger_reaction=False,
        )
        reported = result.posted or result.skipped_reason == "duplicate_idempotency_key"
        if not reported:
            attempts = int(row.get("session_notification_attempts") or 1)
            next_status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            if result.skipped_reason == "session_not_found":
                next_status = "skipped"
            async with pool.acquire() as conn:
                await _finish(
                    conn,
                    run_id=run_id,
                    owner=owner,
                    status=next_status,
                    error=result.skipped_reason or "session report failed",
                )
            return False

    if deploy_status in ACTIONABLE_STATUSES:
        try:
            from app.services.chat_service import trigger_ai_reaction

            await trigger_ai_reaction(str(row["chat_session_id"]), _reaction_prompt(row))
        except Exception as exc:  # noqa: BLE001 - retry boundary for chat reaction providers
            attempts = int(row.get("session_notification_attempts") or 1)
            next_status = "failed" if attempts >= MAX_ATTEMPTS else "reported"
            async with pool.acquire() as conn:
                await _finish(conn, run_id=run_id, owner=owner, status=next_status, error=str(exc))
            return False

    async with pool.acquire() as conn:
        return await _finish(conn, run_id=run_id, owner=owner, status="notified")


async def process_deploy_session_callbacks_once(*, limit: int = 5) -> int:
    """Process one callback batch. Inactive API slots never claim work."""
    from app.services.chat_service import _is_local_active_api_slot

    if not _is_local_active_api_slot():
        return 0
    owner = _owner_instance()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await claim_pending_notifications(conn, owner=owner, limit=limit)
    completed = 0
    for row in rows:
        try:
            completed += int(await process_claimed_notification(row, owner=owner))
        except Exception as exc:
            logger.exception("deploy_session_callback_failed run=%s", row.get("id"))
            attempts = int(row.get("session_notification_attempts") or 1)
            previous = str(row.get("previous_notification_status") or "pending")
            retry_status = "reported" if previous == "reported" else "pending"
            if attempts >= MAX_ATTEMPTS:
                retry_status = "failed"
            async with pool.acquire() as conn:
                await _finish(
                    conn,
                    run_id=int(row["id"]),
                    owner=owner,
                    status=retry_status,
                    error=str(exc),
                )
    return completed


async def deploy_session_callback_poller(interval_seconds: int = 10) -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await process_deploy_session_callbacks_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - long-running poller must degrade gracefully
            logger.warning("deploy_session_callback_poll_failed error=%s", str(exc)[:300])
        await asyncio.sleep(max(2, int(interval_seconds)))
