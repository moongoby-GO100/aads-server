"""
Pipeline Runner API v2 — DB 기반 작업 제출/승인/조회.

보안: 입력 검증(H6), 파라미터화 쿼리(C1), JWT 인증(C2 — main.py 미들웨어)
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

router = APIRouter()
logger = structlog.get_logger(__name__)

# H6 + M4: 허용 프로젝트 화이트리스트
_VALID_PROJECTS = {"AADS", "KIS", "GO100", "SF", "NTV2"}
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_JOB_ID_RE = re.compile(r'^runner-[0-9a-f]{8}$')


class JobSubmitRequest(BaseModel):
    project: str = Field(..., description="프로젝트 코드")
    instruction: str = Field(..., max_length=50000, description="Claude Code에 전달할 지시")
    session_id: Optional[str] = Field(None, description="채팅 세션 ID (보고용)")
    max_cycles: int = Field(3, ge=1, le=10, description="최대 검수 사이클")

    @field_validator('project')
    @classmethod
    def validate_project(cls, v):
        if v not in _VALID_PROJECTS:
            raise ValueError(f"허용 프로젝트: {', '.join(sorted(_VALID_PROJECTS))}")
        return v

    @field_validator('session_id')
    @classmethod
    def validate_session_id(cls, v):
        if v and not _UUID_RE.match(v):
            raise ValueError("session_id는 UUID 형식이어야 합니다")
        return v


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobApproveRequest(BaseModel):
    action: str = Field(..., description="approve 또는 reject")
    feedback: str = Field("", max_length=2000, description="피드백")

    @field_validator('action')
    @classmethod
    def validate_action(cls, v):
        if v not in ("approve", "reject"):
            raise ValueError("action은 approve 또는 reject만 가능")
        return v


@router.post("/pipeline/jobs", response_model=JobSubmitResponse, tags=["pipeline-runner"])
async def submit_job(req: JobSubmitRequest):
    """작업 제출 — DB에 queued 상태로 저장, Runner가 폴링하여 실행."""
    from app.core.db_pool import get_pool
    pool = get_pool()

    job_id = f"runner-{uuid.uuid4().hex[:8]}"
    session_id = req.session_id or ""

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
        raise HTTPException(status_code=500, detail="작업 저장 실패")

    logger.info("pipeline_runner.job_submitted", job_id=job_id, project=req.project)
    return JobSubmitResponse(
        job_id=job_id,
        status="queued",
        message="작업이 대기열에 추가되었습니다. Runner가 곧 실행합니다.",
    )


@router.get("/pipeline/jobs", tags=["pipeline-runner"])
async def list_jobs(
    status: Optional[str] = Query(None, max_length=30),
    project: Optional[str] = Query(None, max_length=10),
    limit: int = Query(20, ge=1, le=100),
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
        if project not in _VALID_PROJECTS:
            raise HTTPException(status_code=400, detail="유효하지 않은 프로젝트")
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
    if not _JOB_ID_RE.match(job_id) and not job_id.startswith("pc-"):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id 형식")

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
    if not _JOB_ID_RE.match(job_id) and not job_id.startswith("pc-"):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id 형식")

    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
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
