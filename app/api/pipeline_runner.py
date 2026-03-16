"""
Pipeline Runner API — DB 기반 작업 제출/승인/조회.

호스트의 pipeline-runner.sh가 DB를 폴링하여 작업을 실행.
aads-server는 제출/승인/상태 조회만 담당.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
logger = structlog.get_logger(__name__)


class JobSubmitRequest(BaseModel):
    project: str = Field(..., description="프로젝트 코드 (AADS, KIS 등)")
    instruction: str = Field(..., description="Claude Code에 전달할 지시")
    session_id: Optional[str] = Field(None, description="채팅 세션 ID (보고용)")
    max_cycles: int = Field(3, description="최대 검수 사이클")


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobApproveRequest(BaseModel):
    action: str = Field(..., description="approve 또는 reject")
    feedback: str = Field("", description="피드백 (reject 시)")


@router.post("/pipeline/jobs", response_model=JobSubmitResponse, tags=["pipeline-runner"])
async def submit_job(req: JobSubmitRequest):
    """작업 제출 — DB에 queued 상태로 저장, Runner가 폴링하여 실행."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    job_id = f"runner-{uuid.uuid4().hex[:8]}"
    session_id = req.session_id or ""

    # 세션 ID가 없으면 프로젝트 워크스페이스의 최근 세션 사용
    if not session_id:
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT s.id::text FROM chat_sessions s
                    JOIN chat_workspaces w ON s.workspace_id = w.id
                    WHERE w.name ILIKE $1
                    ORDER BY s.created_at DESC LIMIT 1
                    """,
                    f"[{req.project}]%",
                )
                if row:
                    session_id = row["id"]
        except Exception as e:
            logger.warning("pipeline_runner.session_lookup_fail", error=str(e))

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pipeline_jobs
                  (job_id, project, instruction, chat_session_id, status, phase, max_cycles, created_at, updated_at)
                VALUES ($1, $2, $3, $4, 'queued', 'queued', $5, NOW(), NOW())
                """,
                job_id, req.project, req.instruction, session_id, req.max_cycles,
            )
    except Exception as e:
        logger.error("pipeline_runner.submit_fail", error=str(e))
        raise HTTPException(status_code=500, detail=f"작업 저장 실패: {e}")

    logger.info("pipeline_runner.job_submitted", job_id=job_id, project=req.project)
    return JobSubmitResponse(
        job_id=job_id,
        status="queued",
        message=f"작업이 대기열에 추가되었습니다. Runner가 곧 실행합니다.",
    )


@router.get("/pipeline/jobs", tags=["pipeline-runner"])
async def list_jobs(
    status: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 20,
):
    """작업 목록 조회."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    conditions = []
    params = []
    idx = 1

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if project:
        conditions.append(f"project = ${idx}")
        params.append(project)
        idx += 1

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT job_id, project, instruction, status, phase, cycle,
                   created_at, updated_at
            FROM pipeline_jobs
            {where}
            ORDER BY created_at DESC
            LIMIT ${idx}
            """,
            *params, limit,
        )

    return [
        {
            "job_id": r["job_id"],
            "project": r["project"],
            "instruction": r["instruction"][:200],
            "status": r["status"],
            "phase": r["phase"],
            "cycle": r["cycle"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.get("/pipeline/jobs/{job_id}", tags=["pipeline-runner"])
async def get_job(job_id: str):
    """작업 상세 조회."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pipeline_jobs WHERE job_id = $1", job_id
        )

    if not row:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다")

    return {
        "job_id": row["job_id"],
        "project": row["project"],
        "instruction": row["instruction"],
        "status": row["status"],
        "phase": row["phase"],
        "cycle": row["cycle"],
        "max_cycles": row["max_cycles"],
        "result_output": row["result_output"],
        "git_diff": (row["git_diff"] or "")[:5000],
        "review_feedback": row["review_feedback"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/pipeline/jobs/{job_id}/approve", tags=["pipeline-runner"])
async def approve_or_reject(job_id: str, req: JobApproveRequest):
    """작업 승인/거부 — Runner가 감지하여 배포 또는 롤백."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        # 원자적 상태 전이
        result = await conn.execute(
            """
            UPDATE pipeline_jobs
            SET status = $2,
                review_feedback = COALESCE(review_feedback, '') || E'\n[CEO] ' || $3,
                updated_at = NOW()
            WHERE job_id = $1 AND status = 'awaiting_approval'
            """,
            job_id,
            "approved" if req.action == "approve" else "rejected",
            req.feedback or req.action,
        )

    affected = int(result.split()[-1]) if result else 0
    if affected == 0:
        raise HTTPException(status_code=400, detail="승인 대기 상태가 아닙니다")

    action_kr = "승인됨" if req.action == "approve" else "거부됨"
    logger.info("pipeline_runner.job_action", job_id=job_id, action=req.action)
    return {"job_id": job_id, "action": req.action, "message": f"작업이 {action_kr}"}
