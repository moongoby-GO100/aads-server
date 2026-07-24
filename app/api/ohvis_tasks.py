"""
OHVIS 3-Tier Response Architecture — Task CRUD API + SSE Events.

POST   /api/v1/ohvis/tasks           — 작업 생성
GET    /api/v1/ohvis/tasks           — 세션별 작업 목록
GET    /api/v1/ohvis/tasks/{task_id} — 단건 조회
PATCH  /api/v1/ohvis/tasks/{task_id} — 상태/단계/결과 업데이트
GET    /api/v1/ohvis/tasks/unreported — 보고 미완료 작업 조회
POST   /api/v1/ohvis/tasks/{task_id}/report — 보고 완료 처리
GET    /api/v1/ohvis/tasks/events/{session_id} — SSE 작업 이벤트 구독
GET    /api/v1/ohvis/tasks/queue     — 멀티태스크 큐 상태
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import asyncpg
import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter()
logger = structlog.get_logger()


def _db_url() -> str:
    return os.getenv("DATABASE_URL", "").replace("postgresql://", "postgres://")


# ─── Pydantic 모델 ────────────────────────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    session_id: UUID
    title: str
    task_type: str = "general"
    steps: list[dict] = Field(default_factory=list)
    runner_job_id: Optional[str] = None
    agent_ids: Optional[list[str]] = None
    parent_turn_id: Optional[UUID] = None


class UpdateTaskRequest(BaseModel):
    status: Optional[str] = None
    steps: Optional[list[dict]] = None
    result: Optional[dict] = None
    ohvis_judgement: Optional[str] = None
    cost_usd: Optional[float] = None


class TaskResponse(BaseModel):
    id: UUID
    session_id: UUID
    title: str
    status: str
    task_type: str
    steps: list
    result: Optional[dict]
    ohvis_judgement: Optional[str]
    runner_job_id: Optional[str]
    agent_ids: Optional[list[str]]
    cost_usd: float
    created_at: str
    updated_at: str
    completed_at: Optional[str]
    reported_at: Optional[str]
    parent_turn_id: Optional[UUID] = None


def _row_to_response(row: asyncpg.Record) -> dict:
    d = dict(row)
    for k in ("created_at", "updated_at", "completed_at", "reported_at"):
        if d.get(k):
            d[k] = d[k].isoformat()
    if isinstance(d.get("steps"), str):
        d["steps"] = json.loads(d["steps"])
    if isinstance(d.get("result"), str):
        d["result"] = json.loads(d["result"])
    return d


# ─── 엔드포인트 ───────────────────────────────────────────────────────────────

@router.post("/ohvis/tasks", status_code=201, tags=["ohvis-tasks"])
async def create_task(req: CreateTaskRequest):
    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO ohvis_tasks (session_id, title, task_type, steps, runner_job_id, agent_ids, parent_turn_id)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
            RETURNING *
            """,
            req.session_id, req.title, req.task_type,
            json.dumps(req.steps), req.runner_job_id, req.agent_ids,
            req.parent_turn_id
        )
        return _row_to_response(row)
    finally:
        await conn.close()


@router.get("/ohvis/tasks", tags=["ohvis-tasks"])
async def list_tasks(
    session_id: UUID = Query(..., description="세션 ID"),
    status: Optional[str] = Query(None, description="상태 필터"),
    limit: int = Query(20, ge=1, le=100),
):
    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        if status:
            rows = await conn.fetch(
                "SELECT * FROM ohvis_tasks WHERE session_id=$1 AND status=$2 ORDER BY created_at DESC LIMIT $3",
                session_id, status, limit
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM ohvis_tasks WHERE session_id=$1 ORDER BY created_at DESC LIMIT $2",
                session_id, limit
            )
        return [_row_to_response(r) for r in rows]
    finally:
        await conn.close()


@router.get("/ohvis/tasks/unreported", tags=["ohvis-tasks"])
async def list_unreported_tasks(
    session_id: Optional[UUID] = Query(None),
):
    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        if session_id:
            rows = await conn.fetch(
                "SELECT * FROM ohvis_tasks WHERE reported_at IS NULL AND status IN ('done','error') AND session_id=$1 ORDER BY completed_at",
                session_id
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM ohvis_tasks WHERE reported_at IS NULL AND status IN ('done','error') ORDER BY completed_at LIMIT 50"
            )
        return [_row_to_response(r) for r in rows]
    finally:
        await conn.close()


@router.get("/ohvis/tasks/events/{session_id}", tags=["ohvis-tasks"])
async def task_events_sse(session_id: UUID, request: Request):
    """P2: Redis pub/sub 기반 작업 이벤트 SSE 스트림."""
    async def event_generator():
        pubsub = None
        try:
            from app.services.redis_stream import _get_redis
            redis = await _get_redis()
            if not redis:
                yield "event: error\ndata: {\"error\": \"redis_unavailable\"}\n\n"
                return
            pubsub = redis.pubsub()
            channel = f"ohvis:task:{session_id}"
            await pubsub.subscribe(channel)
            while True:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg["type"] == "message":
                    data = msg["data"]
                    yield f"data: {data}\n\n"
                else:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("task_sse_stream_error: %s", e)
            yield f"event: error\ndata: {{\"error\": \"{e}\"}}\n\n"
        finally:
            if pubsub:
                try:
                    await pubsub.unsubscribe()
                    await pubsub.close()
                except Exception:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/ohvis/tasks/queue", tags=["ohvis-tasks"])
async def get_task_queue(session_id: UUID = Query(..., description="세션 ID")):
    """P2-1/P2-2: 멀티태스크 큐 상태"""
    from app.services.ohvis_task_manager import get_multi_task_status
    return await get_multi_task_status(str(session_id))


@router.get("/ohvis/tasks/{task_id}", tags=["ohvis-tasks"])
async def get_task(task_id: UUID):
    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        row = await conn.fetchrow("SELECT * FROM ohvis_tasks WHERE id=$1", task_id)
        if not row:
            raise HTTPException(404, "Task not found")
        return _row_to_response(row)
    finally:
        await conn.close()


@router.patch("/ohvis/tasks/{task_id}", tags=["ohvis-tasks"])
async def update_task(task_id: UUID, req: UpdateTaskRequest):
    sets = []
    params = []
    idx = 1

    if req.status is not None:
        sets.append(f"status=${idx}")
        params.append(req.status)
        idx += 1
        if req.status in ("done", "error"):
            sets.append(f"completed_at=${idx}")
            params.append(datetime.now(timezone.utc))
            idx += 1

    if req.steps is not None:
        sets.append(f"steps=${idx}::jsonb")
        params.append(json.dumps(req.steps))
        idx += 1

    if req.result is not None:
        sets.append(f"result=${idx}::jsonb")
        params.append(json.dumps(req.result))
        idx += 1

    if req.ohvis_judgement is not None:
        sets.append(f"ohvis_judgement=${idx}")
        params.append(req.ohvis_judgement)
        idx += 1

    if req.cost_usd is not None:
        sets.append(f"cost_usd=${idx}")
        params.append(req.cost_usd)
        idx += 1

    if not sets:
        raise HTTPException(400, "No fields to update")

    sets.append(f"updated_at=${idx}")
    params.append(datetime.now(timezone.utc))
    idx += 1

    params.append(task_id)
    query = f"UPDATE ohvis_tasks SET {', '.join(sets)} WHERE id=${idx} RETURNING *"

    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        row = await conn.fetchrow(query, *params)
        if not row:
            raise HTTPException(404, "Task not found")
        return _row_to_response(row)
    finally:
        await conn.close()


@router.post("/ohvis/tasks/{task_id}/report", tags=["ohvis-tasks"])
async def mark_reported(task_id: UUID):
    conn = await asyncpg.connect(_db_url(), timeout=5)
    try:
        row = await conn.fetchrow(
            "UPDATE ohvis_tasks SET reported_at=$1, updated_at=$1 WHERE id=$2 RETURNING *",
            datetime.now(timezone.utc), task_id
        )
        if not row:
            raise HTTPException(404, "Task not found")
        return _row_to_response(row)
    finally:
        await conn.close()
