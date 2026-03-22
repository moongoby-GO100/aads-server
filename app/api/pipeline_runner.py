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
_JOB_ID_RE = re.compile(r'^runner-[0-9a-zA-Z_-]+$')


class JobSubmitRequest(BaseModel):
    project: str = Field(..., description="프로젝트 코드")
    instruction: str = Field(..., max_length=50000, description="Claude Code에 전달할 지시")
    session_id: str = Field(..., description="채팅 세션 ID (필수 — 완료 보고 대상)")
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
        if not v or not _UUID_RE.match(v):
            raise ValueError("session_id는 필수이며 UUID 형식이어야 합니다")
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
    session_id = req.session_id  # 필수 필드 — validator에서 이미 검증됨

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
                   error_detail, created_at, updated_at
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
            "error_detail": r.get("error_detail"),
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
        "error_detail": row.get("error_detail"),
        "started_at": row["started_at"].isoformat() if row.get("started_at") else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.post("/pipeline/jobs/{job_id}/notify", tags=["pipeline-runner"])
async def notify_completion(job_id: str):
    """Runner가 작업 완료 시 호출 — 채팅AI에 자동 반응 트리거."""
    if not _JOB_ID_RE.match(job_id) and not job_id.startswith("pc-"):
        raise HTTPException(status_code=400, detail="유효하지 않은 job_id")

    from app.core.db_pool import get_pool
    pool = get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT job_id, project, status, phase, chat_session_id, error_detail, "
            "substring(result_output from 1 for 500) as output_preview, "
            "substring(instruction from 1 for 200) as instruction_preview "
            "FROM pipeline_jobs WHERE job_id = $1", job_id
        )

    if not row:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다")

    session_id = row["chat_session_id"]
    if not session_id or not _UUID_RE.match(session_id):
        return {"status": "skipped", "reason": "session_id 없음"}

    # 채팅AI 자동 반응 트리거
    status = row["status"]
    project = row["project"]
    instruction = row["instruction_preview"] or ""
    output = row["output_preview"] or ""

    if status == "awaiting_approval":
        msg = (f"[시스템] Pipeline Runner 작업 완료 — AI 검수 중\n\n"
               f"**Job**: {job_id}\n**프로젝트**: {project}\n"
               f"**작업**: {instruction}\n**결과 미리보기**:\n{output[:300]}\n\n"
               f"코드를 검수하고 승인/거부를 판단하세요. 승인: pipeline_runner_approve(job_id='{job_id}', action='approve')")
    elif status == "done":
        msg = (f"[시스템] Pipeline Runner 작업 배포 완료\n\n"
               f"**Job**: {job_id}\n**프로젝트**: {project}\n"
               f"**결과**:\n{output[:300]}\n\n"
               f"**배포 검증 5단계 필수 수행:**\n"
               f"1. 컨테이너 상태 확인 (docker ps로 healthy 확인)\n"
               f"2. 변경 파일 반영 확인 (read_remote_file로 핵심 수정 라인 확인)\n"
               f"3. API 헬스체크 (health_check 또는 curl)\n"
               f"4. DB 데이터 정합성 (query_database로 관련 수치 실측 확인)\n"
               f"5. 프론트엔드 변경 시 UI 확인 (browser_snapshot 또는 capture_screenshot)\n"
               f"각 단계를 도구로 실제 확인한 후 결과를 CEO에게 보고하세요. 도구 호출 없이 '정상 완료' 보고 금지.")
    elif status == "error":
        error_detail = row.get("error_detail") or "unknown"
        msg = (f"[시스템] Pipeline Runner 작업 실패\n\n"
               f"**Job**: {job_id}\n**프로젝트**: {project}\n"
               f"**에러 분류**: {error_detail}\n"
               f"**에러**:\n{output[:300]}\n\n"
               f"원인을 진단하고 조치하세요.")
    else:
        msg = f"[시스템] Pipeline Runner 작업 상태 변경: {job_id} → {status}"

    try:
        from app.services.chat_service import trigger_ai_reaction
        import asyncio
        logger.info("pipeline_runner.trigger_sent", job_id=job_id, session_id=session_id, status=status)
        asyncio.create_task(trigger_ai_reaction(session_id, msg))
        return {"status": "triggered", "session_id": session_id}
    except Exception as e:
        logger.warning(f"notify_trigger_failed: {e}")
        return {"status": "error", "detail": str(e)}


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
