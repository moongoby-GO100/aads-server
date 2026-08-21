"""
Reusable chat-session reporting for background jobs and project tools.

Background executors cannot stream into the user's open SSE connection after the
original turn ends.  This module writes a durable assistant message to the bound
chat session instead, so scheduled jobs, runners, and cross-project tools can
report back through the same chat history.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional

import asyncpg

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

_MAX_CONTENT_CHARS = 12000
_MAX_TITLE_CHARS = 160
_XML_TOOL_TAGS = (
    "function_calls",
    "function_response",
    "function_results",
    "tool_results",
    "tool_call",
    "tool_response",
)


@dataclass(frozen=True)
class SessionReportResult:
    posted: bool
    session_id: str
    message_id: Optional[str] = None
    skipped_reason: str = ""


def normalize_session_id(value: Any) -> str:
    """Return a canonical UUID string, or an empty string when invalid/missing."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(uuid.UUID(raw))
    except (TypeError, ValueError):
        return ""


def _sanitize_content(content: str) -> str:
    text = str(content or "")
    for tag in _XML_TOOL_TAGS:
        text = re.sub(rf"<{tag}>.*?</{tag}>", "", text, flags=re.DOTALL)
        text = re.sub(rf"<{tag}>.*", "", text, flags=re.DOTALL)
    text = re.sub(r"<invoke\s+name=[^>]*>.*?</invoke>", "", text, flags=re.DOTALL)
    text = re.sub(r"<invoke\s+name=[^>]*>.*", "", text, flags=re.DOTALL)
    text = text.strip()
    if len(text) > _MAX_CONTENT_CHARS:
        return text[: _MAX_CONTENT_CHARS - 80].rstrip() + "\n\n... [session report truncated]"
    return text


def build_session_report_content(
    *,
    title: str,
    body: Any,
    status: str = "done",
    source: str = "session_report",
    project: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
) -> str:
    """Build a concise CEO-visible report message."""
    normalized_status = (status or "done").strip().lower()
    icon = {
        "done": "✅",
        "success": "✅",
        "ok": "✅",
        "error": "❌",
        "failed": "❌",
        "warning": "⚠️",
        "partial": "⚠️",
        "running": "🔄",
    }.get(normalized_status, "✅")
    safe_title = str(title or "자동 보고").strip()[:_MAX_TITLE_CHARS] or "자동 보고"
    source_label = str(source or "session_report").strip()
    project_label = str(project or "").strip().upper()
    header = f"{icon} **[세션 자동보고]** {safe_title}"
    details = []
    if project_label:
        details.append(f"프로젝트: **{project_label}**")
    if source_label:
        details.append(f"출처: `{source_label}`")
    details.append(f"상태: `{normalized_status}`")

    if isinstance(body, (dict, list, tuple)):
        rendered_body = json.dumps(body, ensure_ascii=False, default=str, indent=2)
    else:
        rendered_body = str(body or "")
    rendered_body = _sanitize_content(rendered_body)

    meta_text = ""
    if metadata:
        compact_meta = json.dumps(dict(metadata), ensure_ascii=False, default=str)
        if compact_meta and compact_meta != "{}":
            meta_text = f"\n\n`metadata`: `{compact_meta[:1000]}`"

    if "\n" in rendered_body or len(rendered_body) > 160:
        body_text = f"\n\n```text\n{rendered_body}\n```" if rendered_body else ""
    else:
        body_text = f"\n\n{rendered_body}" if rendered_body else ""
    return "\n".join([header, *details]) + body_text + meta_text


async def post_session_report(
    *,
    session_id: Any,
    title: str,
    body: Any,
    status: str = "done",
    source: str = "session_report",
    project: str = "",
    metadata: Optional[Mapping[str, Any]] = None,
    model_used: Optional[str] = None,
    intent: str = "auto_report",
    idempotency_key: Optional[str] = None,
    conn: Optional[asyncpg.Connection] = None,
) -> SessionReportResult:
    """Persist an assistant report message into a chat session.

    `conn` may be supplied by callers already inside a DB workflow.  When omitted,
    the app's shared asyncpg pool is used.
    """
    sid = normalize_session_id(session_id)
    if not sid:
        return SessionReportResult(posted=False, session_id="", skipped_reason="missing_or_invalid_session_id")

    content = build_session_report_content(
        title=title,
        body=body,
        status=status,
        source=source,
        project=project,
        metadata=metadata,
    )

    async def _insert(target_conn: asyncpg.Connection) -> SessionReportResult:
        async with target_conn.transaction():
            exists = await target_conn.fetchval(
                "SELECT 1 FROM chat_sessions WHERE id = $1::uuid LIMIT 1",
                sid,
            )
            if not exists:
                return SessionReportResult(posted=False, session_id=sid, skipped_reason="session_not_found")

            if idempotency_key:
                row = await target_conn.fetchrow(
                    """
                    INSERT INTO chat_messages
                        (session_id, role, content, model_used, intent, cost,
                         tokens_in, tokens_out, attachments, sources, tools_called, idempotency_key)
                    VALUES ($1::uuid, 'assistant', $2, $3, $4, $5,
                            0, 0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, $6)
                    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
                    RETURNING id
                    """,
                    sid,
                    content,
                    model_used,
                    intent,
                    Decimal("0"),
                    idempotency_key[:64],
                )
                if row is None:
                    return SessionReportResult(posted=False, session_id=sid, skipped_reason="duplicate_idempotency_key")
            else:
                row = await target_conn.fetchrow(
                    """
                    INSERT INTO chat_messages
                        (session_id, role, content, model_used, intent, cost,
                         tokens_in, tokens_out, attachments, sources, tools_called)
                    VALUES ($1::uuid, 'assistant', $2, $3, $4, $5,
                            0, 0, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb)
                    RETURNING id
                    """,
                    sid,
                    content,
                    model_used,
                    intent,
                    Decimal("0"),
                )
            await target_conn.execute(
                "UPDATE chat_sessions SET message_count = message_count + 1, updated_at = NOW() WHERE id = $1::uuid",
                sid,
            )
        message_id = str(row["id"]) if row and row.get("id") else None
        logger.info("session_report_posted session=%s source=%s title=%s", sid[:8], source, title[:80])
        return SessionReportResult(posted=True, session_id=sid, message_id=message_id)

    try:
        if conn is not None:
            return await _insert(conn)
        pool = get_pool()
        async with pool.acquire() as acquired:
            return await _insert(acquired)
    except Exception as exc:
        logger.warning("session_report_post_failed session=%s source=%s error=%s", sid[:8], source, exc)
        return SessionReportResult(posted=False, session_id=sid, skipped_reason=str(exc)[:300])
