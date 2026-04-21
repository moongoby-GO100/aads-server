"""
AADS-127: Debate Logs API
GET /api/v1/debate-logs?project_id={id} — 토론 이력 조회
"""
import os
import json
from datetime import timezone, timedelta
from typing import Optional

import asyncpg
import structlog
from fastapi import APIRouter, HTTPException, Query

logger = structlog.get_logger()
router = APIRouter()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads:aads2026secure@aads-postgres:5432/aads",
)

KST = timezone(timedelta(hours=9))


async def _get_conn():
    return await asyncpg.connect(DATABASE_URL, timeout=10)


@router.get("/debate-logs")
async def get_debate_logs(
    project_id: Optional[str] = Query(None, description="프로젝트 UUID (없으면 전체)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """프로젝트별(또는 전체) 토론 이력 조회."""
    try:
        conn = await _get_conn()
        try:
            if project_id:
                rows = await conn.fetch(
                    """
                    SELECT id, project_id, round_number,
                           strategist_message, planner_message,
                           consensus_reached, escalated, created_at
                    FROM debate_logs
                    WHERE project_id = $1::uuid
                    ORDER BY round_number ASC, created_at ASC
                    LIMIT $2 OFFSET $3
                    """,
                    project_id, limit, offset,
                )
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM debate_logs WHERE project_id = $1::uuid",
                    project_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, project_id, round_number,
                           strategist_message, planner_message,
                           consensus_reached, escalated, created_at
                    FROM debate_logs
                    ORDER BY created_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit, offset,
                )
                total = await conn.fetchval("SELECT COUNT(*) FROM debate_logs")
        finally:
            await conn.close()

        items = []
        for row in rows:
            items.append({
                "id": row["id"],
                "project_id": str(row["project_id"]) if row["project_id"] else None,
                "round_number": row["round_number"],
                "strategist_message": json.loads(row["strategist_message"]) if row["strategist_message"] else {},
                "planner_message": json.loads(row["planner_message"]) if row["planner_message"] else {},
                "consensus_reached": row["consensus_reached"],
                "escalated": row["escalated"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            })

        logger.info("debate_logs_fetched", project_id=project_id, count=len(items))
        return {
            "project_id": project_id or "all",
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        }

    except asyncpg.InvalidTextRepresentationError:
        raise HTTPException(status_code=400, detail="Invalid project_id format (UUID expected)")
    except Exception as e:
        logger.error("debate_logs_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
