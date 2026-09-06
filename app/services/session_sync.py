"""세션 간 작업 브로드캐스트 — 러너 완료/장애를 관련 세션에 자동 전파."""
from __future__ import annotations

import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


async def broadcast_runner_completion(
    job_id: str,
    project: str,
    status: str,
    summary: str,
    source_session_id: Optional[str] = None,
) -> None:
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            ceo_session = await conn.fetchrow(
                "SELECT cs.id FROM chat_sessions cs "
                "JOIN chat_workspaces cw ON cs.workspace_id = cw.id "
                "WHERE cw.name LIKE '%CEO%' OR cw.name LIKE '%통합%' "
                "ORDER BY cs.updated_at DESC LIMIT 1"
            )

            project_session = await conn.fetchrow(
                "SELECT cs.id FROM chat_sessions cs "
                "JOIN chat_workspaces cw ON cs.workspace_id = cw.id "
                "WHERE cw.name LIKE $1 "
                "ORDER BY cs.updated_at DESC LIMIT 1",
                f"%{project}%",
            )

            icon = {"done": "✅", "error": "❌", "deployed": "🚀"}.get(status, "⚠️")
            msg = f"{icon} **[러너 완료 알림]** `{job_id}` ({project})\n{summary}"

            if ceo_session and str(ceo_session["id"]) != source_session_id:
                await _insert_system_message(conn, str(ceo_session["id"]), msg)

            if project_session and str(project_session["id"]) != source_session_id:
                if not ceo_session or str(project_session["id"]) != str(ceo_session["id"]):
                    await _insert_system_message(conn, str(project_session["id"]), msg)

            logger.info("broadcast_sent job=%s project=%s status=%s", job_id, project, status)
    except Exception as e:
        logger.warning("broadcast_runner_completion_failed: %s", e)


async def broadcast_goal_event(
    goal_id: str,
    project: str,
    event: str,
    detail: str,
) -> None:
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            ceo_session = await conn.fetchrow(
                "SELECT cs.id FROM chat_sessions cs "
                "JOIN chat_workspaces cw ON cs.workspace_id = cw.id "
                "WHERE cw.name LIKE '%CEO%' OR cw.name LIKE '%통합%' "
                "ORDER BY cs.updated_at DESC LIMIT 1"
            )
            if ceo_session:
                icon = {"milestone_completed": "🎯", "goal_completed": "🏆", "overdue": "⏰"}.get(event, "📢")
                msg = f"{icon} **[{event}]** ({project})\n{detail}"
                await _insert_system_message(conn, str(ceo_session["id"]), msg)
    except Exception as e:
        logger.warning("broadcast_goal_event_failed: %s", e)


async def _insert_system_message(conn, session_id: str, content: str) -> None:
    await conn.execute(
        "INSERT INTO chat_messages (id, session_id, role, content, created_at) "
        "VALUES ($1, $2, 'system', $3, NOW())",
        str(uuid.uuid4()), session_id, content,
    )
