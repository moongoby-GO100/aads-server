"""Goals API — 목표 Control Loop 엔드포인트."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()


class GoalCreateRequest(BaseModel):
    project: str
    title: str
    priority: str = "P2"
    success_criteria: Optional[str] = None
    parent_goal_id: Optional[str] = None


@router.get("/goals")
async def list_goals(project: Optional[str] = Query(None)):
    from app.core.db_pool import get_pool
    pool = get_pool()
    async with pool.acquire() as conn:
        if project:
            rows = await conn.fetch(
                """
                SELECT id, project, title, priority, status, created_at, completed_at
                FROM goals WHERE project = $1 ORDER BY created_at DESC
                """,
                project,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, project, title, priority, status, created_at, completed_at FROM goals ORDER BY created_at DESC"
            )
    return [
        {
            "goal_id": str(r["id"]),
            "project": r["project"],
            "title": r["title"],
            "priority": r["priority"],
            "status": r["status"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
        }
        for r in rows
    ]


@router.post("/goals")
async def create_goal(req: GoalCreateRequest):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.create_goal(
        project=req.project,
        title=req.title,
        priority=req.priority,
        success_criteria=req.success_criteria,
        parent_goal_id=req.parent_goal_id,
    )
    return result


@router.get("/goals/{goal_id}/status")
async def goal_status(goal_id: str):
    from app.services.goal_manager import goal_state_machine
    result = await goal_state_machine.get_goal_status(goal_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result
