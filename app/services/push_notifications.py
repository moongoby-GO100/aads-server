"""OHVIS web push notification service.

The service is intentionally best-effort: notification failures must never block
chat response persistence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Optional

import asyncpg

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

PUSH_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS app_push_subscriptions (
    id UUID PRIMARY KEY,
    tenant_id UUID NULL,
    user_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    subscription JSONB NOT NULL,
    user_agent TEXT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_success_at TIMESTAMPTZ NULL,
    last_error TEXT NULL,
    last_error_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE NULLS NOT DISTINCT (tenant_id, user_id, endpoint)
);
CREATE INDEX IF NOT EXISTS idx_app_push_subscriptions_user_enabled
    ON app_push_subscriptions(tenant_id, user_id, enabled);
"""

_schema_ready = False
_schema_lock: Optional[asyncio.Lock] = None


def _get_schema_lock() -> asyncio.Lock:
    global _schema_lock
    if _schema_lock is None:
        _schema_lock = asyncio.Lock()
    return _schema_lock


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def vapid_public_key() -> str:
    return _env("OHVIS_WEB_PUSH_VAPID_PUBLIC_KEY") or _env("AADS_WEB_PUSH_VAPID_PUBLIC_KEY")


def vapid_private_key() -> str:
    return _env("OHVIS_WEB_PUSH_VAPID_PRIVATE_KEY") or _env("AADS_WEB_PUSH_VAPID_PRIVATE_KEY")


def vapid_claims() -> dict[str, str]:
    subject = _env("OHVIS_WEB_PUSH_SUBJECT") or _env("AADS_WEB_PUSH_SUBJECT") or "mailto:admin@newtalk.kr"
    return {"sub": subject}


def is_web_push_configured() -> bool:
    return bool(vapid_public_key() and vapid_private_key())


async def ensure_push_schema(conn: Optional[asyncpg.Connection] = None) -> None:
    global _schema_ready
    if _schema_ready:
        return
    async with _get_schema_lock():
        if _schema_ready:
            return
        if conn is not None:
            await conn.execute(PUSH_TABLE_DDL)
        else:
            async with get_pool().acquire() as schema_conn:
                await schema_conn.execute(PUSH_TABLE_DDL)
        _schema_ready = True


def _user_id(current_user: dict[str, Any]) -> str:
    user_id = str(current_user.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("push_user_id_required")
    return user_id


def _tenant_id(current_user: dict[str, Any]) -> Optional[uuid.UUID]:
    raw = current_user.get("tenant_id")
    return uuid.UUID(str(raw)) if raw else None


async def upsert_subscription(
    *,
    current_user: dict[str, Any],
    subscription: dict[str, Any],
    user_agent: str = "",
) -> dict[str, Any]:
    endpoint = str(subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("invalid_push_subscription")

    await ensure_push_schema()
    uid = _user_id(current_user)
    tid = _tenant_id(current_user)
    sub_json = json.dumps(subscription, ensure_ascii=False)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO app_push_subscriptions (
                id, tenant_id, user_id, endpoint, subscription, user_agent, enabled, updated_at
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, TRUE, NOW())
            ON CONFLICT (tenant_id, user_id, endpoint) DO UPDATE
               SET subscription = EXCLUDED.subscription,
                   user_agent = EXCLUDED.user_agent,
                   enabled = TRUE,
                   last_error = NULL,
                   last_error_at = NULL,
                   updated_at = NOW()
            RETURNING id::text, enabled, updated_at
            """,
            uuid.uuid4(),
            tid,
            uid,
            endpoint,
            sub_json,
            user_agent[:500],
        )
    return {"id": row["id"], "enabled": row["enabled"], "updated_at": row["updated_at"].isoformat()}


async def disable_subscription(*, current_user: dict[str, Any], endpoint: str) -> bool:
    await ensure_push_schema()
    uid = _user_id(current_user)
    tid = _tenant_id(current_user)
    async with get_pool().acquire() as conn:
        result = await conn.execute(
            """
            UPDATE app_push_subscriptions
               SET enabled = FALSE, updated_at = NOW()
             WHERE tenant_id IS NOT DISTINCT FROM $1
               AND user_id = $2
               AND endpoint = $3
            """,
            tid,
            uid,
            endpoint,
        )
    return result.endswith("1")


async def _mark_subscription_result(
    conn: asyncpg.Connection,
    subscription_id: uuid.UUID,
    *,
    success: bool,
    error: str = "",
    disable: bool = False,
) -> None:
    if success:
        await conn.execute(
            """
            UPDATE app_push_subscriptions
               SET last_success_at = NOW(), last_error = NULL, last_error_at = NULL, updated_at = NOW()
             WHERE id = $1
            """,
            subscription_id,
        )
        return
    await conn.execute(
        """
        UPDATE app_push_subscriptions
           SET last_error = $2,
               last_error_at = NOW(),
               enabled = CASE WHEN $3 THEN FALSE ELSE enabled END,
               updated_at = NOW()
         WHERE id = $1
        """,
        subscription_id,
        error[:1000],
        disable,
    )


async def send_web_push_to_user(
    *,
    tenant_id: Optional[str],
    user_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not is_web_push_configured():
        return {"sent": 0, "failed": 0, "skipped": "vapid_not_configured"}

    await ensure_push_schema()
    tid = uuid.UUID(str(tenant_id)) if tenant_id else None
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, subscription
              FROM app_push_subscriptions
             WHERE tenant_id IS NOT DISTINCT FROM $1
               AND user_id = $2
               AND enabled = TRUE
             ORDER BY updated_at DESC
             LIMIT 20
            """,
            tid,
            str(user_id),
        )

    if not rows:
        return {"sent": 0, "failed": 0, "skipped": "no_enabled_subscriptions"}

    try:
        from pywebpush import WebPushException, webpush
    except Exception as import_error:
        logger.warning("web_push_dependency_missing: %s", import_error)
        return {"sent": 0, "failed": 0, "skipped": "pywebpush_not_installed"}

    payload_text = json.dumps(payload, ensure_ascii=False)
    sent = 0
    failed = 0
    async with get_pool().acquire() as conn:
        for row in rows:
            sub = row["subscription"]
            if isinstance(sub, str):
                sub = json.loads(sub)
            try:
                await asyncio.to_thread(
                    webpush,
                    subscription_info=sub,
                    data=payload_text,
                    vapid_private_key=vapid_private_key(),
                    vapid_claims=vapid_claims(),
                    timeout=10,
                )
                sent += 1
                await _mark_subscription_result(conn, row["id"], success=True)
            except WebPushException as exc:
                failed += 1
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                await _mark_subscription_result(
                    conn,
                    row["id"],
                    success=False,
                    error=f"webpush:{status_code or 'unknown'}:{exc}",
                    disable=status_code in {404, 410},
                )
            except Exception as exc:
                failed += 1
                await _mark_subscription_result(
                    conn,
                    row["id"],
                    success=False,
                    error=f"{type(exc).__name__}:{exc}",
                )
    return {"sent": sent, "failed": failed}


async def notify_chat_response_complete(
    *,
    session_id: str,
    assistant_message_id: str,
    fallback_user_id: Optional[str] = None,
    body: str = "응답이 완료되었습니다.",
) -> dict[str, Any]:
    try:
        await ensure_push_schema()
        sid = uuid.UUID(str(session_id))
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id::text, tenant_id::text, user_id, title
                  FROM chat_sessions
                 WHERE id = $1
                """,
                sid,
            )
        if not row:
            return {"sent": 0, "failed": 0, "skipped": "session_not_found"}
        uid = str(row["user_id"] or fallback_user_id or "").strip()
        if not uid:
            return {"sent": 0, "failed": 0, "skipped": "session_user_missing"}
        title = str(row["title"] or "오비스").strip()
        payload = {
            "title": "오비스",
            "body": f"{title}: {body}"[:240],
            "url": f"/chat#{session_id}",
            "tag": f"chat-complete-{session_id}",
            "data": {
                "session_id": session_id,
                "message_id": assistant_message_id,
                "event": "chat_response_complete",
            },
        }
        return await send_web_push_to_user(
            tenant_id=row["tenant_id"],
            user_id=uid,
            payload=payload,
        )
    except Exception as exc:
        logger.warning("chat_response_push_notify_failed session=%s error=%s", session_id[:8], exc)
        return {"sent": 0, "failed": 1, "error": str(exc)[:300]}
