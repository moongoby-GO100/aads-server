"""
실시간 작업 모니터 API — Pipeline B/C 진행 상황 조회 + SSE 스트리밍.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/tasks/active")
async def get_active_tasks(session_id: str = Query(default="")):
    """현재 세션 또는 전체의 활성/최근완료 작업 목록."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            # Pipeline Runner + Legacy C (pipeline_jobs)
            if session_id:
                # 해당 세션 작업만 + 활성 전체
                pc_rows = await conn.fetch(
                    """
                    SELECT job_id AS task_id, project, instruction AS title,
                           CASE WHEN job_id LIKE 'runner-%' THEN 'runner' ELSE 'pipeline_c' END AS pipeline,
                           phase, status, created_at, updated_at
                    FROM pipeline_jobs
                    WHERE (chat_session_id = $1
                           OR status IN ('running','claimed','queued'))
                      AND (status IN ('running','awaiting_approval','queued','claimed','approved')
                           OR updated_at > NOW() - interval '1 hour')
                    ORDER BY created_at DESC
                    LIMIT 20
                    """,
                    session_id,
                )
            else:
                pc_rows = await conn.fetch(
                    """
                    SELECT job_id AS task_id, project, instruction AS title,
                           CASE WHEN job_id LIKE 'runner-%' THEN 'runner' ELSE 'pipeline_c' END AS pipeline,
                           phase, status, created_at, updated_at
                    FROM pipeline_jobs
                    WHERE status IN ('running','awaiting_approval','queued','claimed','approved')
                       OR updated_at > NOW() - interval '1 hour'
                    ORDER BY created_at DESC
                    LIMIT 20
                    """,
                )
            # Pipeline B (directive_lifecycle) — 활성 작업만 (전체 세션 공유)
            pb_rows = await conn.fetch(
                """
                SELECT task_id, project,
                       title, 'agent' AS pipeline,
                       status AS phase, status,
                       created_at, completed_at AS updated_at
                FROM directive_lifecycle
                WHERE executor = 'autonomous_executor'
                  AND (status = 'in_progress'
                       OR completed_at > NOW() - interval '10 minutes')
                ORDER BY created_at DESC
                LIMIT 10
                """,
            )

        tasks = []
        for r in list(pc_rows) + list(pb_rows):
            created = r["created_at"]
            elapsed = int((datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc if created.tzinfo is None else created.tzinfo)).total_seconds()) if created else 0
            tasks.append({
                "task_id": r["task_id"],
                "project": r["project"] or "",
                "title": (r["title"] or "")[:200],
                "pipeline": r["pipeline"],
                "phase": r["phase"] or "",
                "status": r["status"] or "",
                "elapsed_sec": max(0, elapsed),
                "created_at": created.isoformat() if created else "",
            })
        return {"tasks": tasks}
    except Exception as e:
        logger.error(f"[TaskMonitor] active tasks error: {e}")
        return {"tasks": [], "error": str(e)}


@router.get("/tasks/{task_id}/logs")
async def get_task_logs(
    task_id: str,
    since: str = Query(default=""),
    last_n: int = Query(default=50, ge=1, le=200),
    log_type: str = Query(default=""),
):
    """특정 작업의 로그 조회."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            conditions = ["task_id = $1"]
            params: list = [task_id]
            idx = 2

            if since:
                conditions.append(f"created_at > ${idx}::timestamptz")
                params.append(since)
                idx += 1
            if log_type:
                conditions.append(f"log_type = ${idx}")
                params.append(log_type)
                idx += 1

            where = " AND ".join(conditions)
            rows = await conn.fetch(
                f"""
                SELECT id, log_type, content, phase, metadata, created_at
                FROM task_logs
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT {last_n}
                """,
                *params,
            )
        logs = [
            {
                "id": r["id"],
                "log_type": r["log_type"],
                "content": r["content"],
                "phase": r["phase"] or "",
                "metadata": json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {}),
                "created_at": r["created_at"].isoformat(),
            }
            for r in reversed(rows)  # 시간순 정렬
        ]
        return {"task_id": task_id, "logs": logs}
    except Exception as e:
        logger.error(f"[TaskMonitor] logs error: {e}")
        return {"task_id": task_id, "logs": [], "error": str(e)}


@router.get("/tasks/{task_id}/stream")
async def stream_task_logs(task_id: str):
    """SSE 스트림 — 특정 작업의 실시간 로그."""
    from app.services.task_logger import subscribe, unsubscribe

    async def event_generator():
        q = subscribe(task_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe(task_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
