"""
AADS-122: 교훈(Lessons) CRUD API
- POST /api/v1/lessons        — 교훈 등록
- GET  /api/v1/lessons        — 전체 목록 (category/project/severity 필터)
- GET  /api/v1/lessons/{id}   — 개별 조회
"""
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import asyncpg

logger = structlog.get_logger()
router = APIRouter()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads:aads_dev_local@aads-postgres:5432/aads"
)

KST = timezone(timedelta(hours=9))


async def _get_conn():
    return await asyncpg.connect(DATABASE_URL, timeout=10)


# ─── Models ──────────────────────────────────────────────────────────────────

class LessonCreate(BaseModel):
    id: str
    title: str
    category: str
    source_project: str
    source_task: Optional[str] = None
    severity: str = "normal"
    summary: str
    file_path: Optional[str] = None
    applicable_to: Optional[str] = None


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.post("/lessons")
async def create_lesson(req: LessonCreate):
    """교훈 등록."""
    try:
        conn = await _get_conn()
        try:
            await conn.execute(
                """
                INSERT INTO lessons
                    (id, title, category, source_project, source_task,
                     severity, summary, file_path, applicable_to,
                     created_at, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW(),NOW())
                """,
                req.id, req.title, req.category, req.source_project,
                req.source_task, req.severity, req.summary,
                req.file_path, req.applicable_to,
            )
        finally:
            await conn.close()
        logger.info("lesson_created", id=req.id, category=req.category)
        return {"ok": True, "id": req.id}
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail=f"Lesson {req.id} already exists")
    except Exception as e:
        logger.error("lesson_create_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/lessons")
async def list_lessons(
    category: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
):
    """교훈 목록 조회 (필터 옵션: category, project, severity)."""
    conditions = []
    params: List = []
    idx = 1

    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1
    if project:
        conditions.append(f"source_project = ${idx}")
        params.append(project)
        idx += 1
    if severity:
        conditions.append(f"severity = ${idx}")
        params.append(severity)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT id, title, category, source_project, source_task,
               severity, summary, file_path, applicable_to,
               created_at, updated_at
        FROM lessons
        {where}
        ORDER BY id
    """
    try:
        conn = await _get_conn()
        try:
            rows = await conn.fetch(sql, *params)
        finally:
            await conn.close()
        lessons = [dict(r) for r in rows]
        # datetime → ISO string
        for l in lessons:
            for k in ("created_at", "updated_at"):
                if l[k]:
                    l[k] = l[k].isoformat()
        return {"total": len(lessons), "lessons": lessons}
    except Exception as e:
        logger.error("lesson_list_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/lessons/{lesson_id}")
async def get_lesson(lesson_id: str):
    """교훈 개별 조회."""
    try:
        conn = await _get_conn()
        try:
            row = await conn.fetchrow(
                """
                SELECT id, title, category, source_project, source_task,
                       severity, summary, file_path, applicable_to,
                       created_at, updated_at
                FROM lessons WHERE id = $1
                """,
                lesson_id,
            )
        finally:
            await conn.close()
        if not row:
            raise HTTPException(status_code=404, detail=f"Lesson {lesson_id} not found")
        data = dict(row)
        for k in ("created_at", "updated_at"):
            if data[k]:
                data[k] = data[k].isoformat()
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error("lesson_get_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
