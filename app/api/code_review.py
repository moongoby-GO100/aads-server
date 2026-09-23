"""
AI-to-AI 피드백 시스템 — Code Review API 엔드포인트.
Pipeline Runner(bash)가 curl로 호출.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/review", tags=["code-review"])


class CodeReviewRequest(BaseModel):
    """코드 리뷰 요청."""
    job_id: str = Field(..., description="Pipeline Runner 작업 ID")
    project: str = Field(..., description="프로젝트명")
    diff: str = Field(..., description="git diff 내용")
    instruction: str = Field("", description="원본 작업 지시")
    files_changed: list[str] = Field(default_factory=list, description="변경된 파일 목록")


class CodeReviewResponse(BaseModel):
    """코드 리뷰 결과."""
    verdict: str
    score: float
    feedback: dict
    issues: list
    flag_category: str | None = None
    failure_stage: str | None = None
    needs_retry: bool = False
    model_used: str | None = None


class AsyncCodeReviewRequest(CodeReviewRequest):
    """Durable review request. Clients may provide an id for safe retries."""
    request_id: UUID | None = None


class AsyncCodeReviewAccepted(BaseModel):
    request_id: UUID
    status: str
    result_url: str


_review_tasks: set[asyncio.Task] = set()
_review_concurrency = asyncio.Semaphore(2)


def _review_deadlines() -> tuple[int, int]:
    """Keep worker and stale-claim limits below/above one another."""
    from app.services.code_reviewer import _REVIEW_ASYNC_DEADLINE_SEC

    hard_limit = int(_REVIEW_ASYNC_DEADLINE_SEC) + 10
    stale_after = max(
        hard_limit + 60,
        int(os.environ.get("REVIEW_REQUEST_STALE_SEC", "600")),
    )
    return hard_limit, stale_after


def _payload_sha256(req: CodeReviewRequest) -> str:
    payload = {
        "job_id": req.job_id,
        "project": req.project,
        "diff": req.diff,
        "instruction": req.instruction,
        "files_changed": req.files_changed,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _schedule_review_request(request_id: UUID) -> None:
    task = asyncio.create_task(_execute_review_request(request_id))
    _review_tasks.add(task)
    task.add_done_callback(_review_tasks.discard)


async def _execute_review_request(request_id: UUID) -> None:
    """Claim one persisted request, run it once, then persist the verdict."""
    from app.core.db_pool import get_pool
    from app.services.code_reviewer import review_code_diff as do_review

    scheduled_at = time.monotonic()
    async with _review_concurrency:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE code_review_requests
                SET status='running', attempts=attempts + 1,
                    started_at=COALESCE(started_at, NOW()), updated_at=NOW(),
                    error_detail=NULL
                WHERE request_id=$1 AND status='queued'
                RETURNING job_id, project, diff, instruction, files_changed,
                          created_at, started_at, attempts
                """,
                request_id,
            )
        if not row:
            return

        queued_wait_ms = max(
            0,
            int((row["started_at"] - row["created_at"]).total_seconds() * 1000),
        )
        logger.info(
            "review_measurement label=review.queue.claim path=async stage=queue "
            "outcome=claimed request_id=%s job_id=%s queued_wait_ms=%s "
            "worker_wait_ms=%s",
            request_id, row["job_id"], queued_wait_ms,
            int((time.monotonic() - scheduled_at) * 1000),
        )
        run_started_at = time.monotonic()

        try:
            files_changed = row["files_changed"]
            if isinstance(files_changed, str):
                files_changed = json.loads(files_changed)
            # 이 경로는 202 로 이미 반환했고 클라이언트는 request_id 를 폴링한다.
            # 프록시(Cloudflare ~100초)에 묶이지 않으므로 동기 경로보다 긴 마감을
            # 준다. 재검수 스위퍼가 동기 경로와 똑같은 상한에 걸리면 복구가 안 된다.
            from app.services.code_reviewer import _REVIEW_ASYNC_DEADLINE_SEC

            hard_limit, _ = _review_deadlines()
            result = await asyncio.wait_for(
                do_review(
                    project=row["project"],
                    job_id=row["job_id"],
                    diff=row["diff"],
                    instruction=row["instruction"] or "",
                    files_changed=list(files_changed or []),
                    deadline_sec=_REVIEW_ASYNC_DEADLINE_SEC,
                ),
                timeout=hard_limit,
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE code_review_requests
                    SET status='completed', verdict=$2, score=$3,
                        feedback=$4::jsonb, issues=$5::jsonb,
                        flag_category=$6, failure_stage=$7,
                        needs_retry=$8, model_used=$9,
                        completed_at=NOW(), updated_at=NOW()
                    WHERE request_id=$1 AND status='running' AND attempts=$10
                    """,
                    request_id,
                    result.verdict,
                    result.score,
                    json.dumps(result.feedback, ensure_ascii=False),
                    json.dumps(result.issues, ensure_ascii=False),
                    result.flag_category,
                    result.failure_stage,
                    result.needs_retry,
                    result.model_used,
                    row["attempts"],
                )
            logger.info(
                "review_measurement label=review.request.completed path=async stage=request "
                "outcome=completed request_id=%s job_id=%s run_ms=%s total_ms=%s "
                "queued_wait_ms=%s",
                request_id, row["job_id"],
                int((time.monotonic() - run_started_at) * 1000),
                int((time.monotonic() - scheduled_at) * 1000), queued_wait_ms,
            )
        except Exception as exc:
            logger.exception(
                "review_measurement label=review.request.failed path=async stage=request "
                "outcome=error request_id=%s job_id=%s run_ms=%s total_ms=%s",
                request_id, row["job_id"],
                int((time.monotonic() - run_started_at) * 1000),
                int((time.monotonic() - scheduled_at) * 1000),
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE code_review_requests
                    SET status='failed', error_detail=$2,
                        needs_retry=TRUE, completed_at=NOW(), updated_at=NOW()
                    WHERE request_id=$1 AND status='running' AND attempts=$3
                    """,
                    request_id,
                    str(exc)[:1000],
                    row["attempts"],
                )


async def _resume_stored_request(request_id: UUID) -> str:
    """Reclaim only expired/failed work; the saved payload is never rebuilt."""
    from app.core.db_pool import get_pool

    _, stale_after = _review_deadlines()
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE code_review_requests
               SET status='queued', started_at=NULL, completed_at=NULL,
                   error_detail='stale async review recovered', updated_at=NOW()
             WHERE request_id=$1
               AND (status='failed' OR
                    (status='running' AND updated_at < NOW() - ($2::int * INTERVAL '1 second')))
            RETURNING status
            """,
            request_id,
            stale_after,
        )
        if not row:
            row = await conn.fetchrow(
                "SELECT status FROM code_review_requests WHERE request_id=$1",
                request_id,
            )
    if not row:
        raise HTTPException(status_code=404, detail="리뷰 요청을 찾을 수 없습니다")
    request_status = row["status"]
    if request_status == "queued":
        _schedule_review_request(request_id)
    return request_status


@router.post("/code-diff", response_model=CodeReviewResponse)
async def review_code_diff(req: CodeReviewRequest):
    """코드 diff를 AI Reviewer로 리뷰."""
    try:
        from app.services.code_reviewer import review_code_diff as do_review
        result = await do_review(
            project=req.project,
            job_id=req.job_id,
            diff=req.diff,
            instruction=req.instruction,
            files_changed=req.files_changed,
        )
        return CodeReviewResponse(
            verdict=result.verdict,
            score=result.score,
            feedback=result.feedback,
            issues=result.issues,
            flag_category=result.flag_category,
            failure_stage=result.failure_stage,
            needs_retry=result.needs_retry,
            model_used=result.model_used,
        )
    except Exception as e:
        logger.error(f"code_review_api_error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/code-diff/requests",
    response_model=AsyncCodeReviewAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_code_review_request(req: AsyncCodeReviewRequest):
    """Persist a review request before work starts and return immediately."""
    from app.core.db_pool import get_pool

    request_id = req.request_id or uuid4()
    payload_hash = _payload_sha256(req)
    pool = get_pool()
    async with pool.acquire() as conn:
        inserted = await conn.fetchval(
            """
            INSERT INTO code_review_requests
                (request_id, job_id, project, diff, instruction, files_changed, payload_sha256)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            ON CONFLICT (request_id) DO NOTHING
            RETURNING request_id
            """,
            request_id,
            req.job_id,
            req.project,
            req.diff,
            req.instruction,
            json.dumps(req.files_changed, ensure_ascii=False),
            payload_hash,
        )
        row = await conn.fetchrow(
            "SELECT status, payload_sha256 FROM code_review_requests WHERE request_id=$1",
            request_id,
        )
    if not row:
        raise HTTPException(status_code=500, detail="리뷰 요청 저장에 실패했습니다")
    if row["payload_sha256"] != payload_hash:
        raise HTTPException(status_code=409, detail="request_id가 다른 payload에 이미 사용되었습니다")
    if inserted or row["status"] == "queued":
        _schedule_review_request(request_id)
    return AsyncCodeReviewAccepted(
        request_id=request_id,
        status=row["status"],
        result_url=f"/api/v1/review/code-diff/requests/{request_id}",
    )


@router.post(
    "/code-diff/requests/{request_id}/resume",
    response_model=AsyncCodeReviewAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_code_review_request(request_id: UUID):
    """Resume a durable request using its exact stored payload only."""
    request_status = await _resume_stored_request(request_id)
    return AsyncCodeReviewAccepted(
        request_id=request_id,
        status=request_status,
        result_url=f"/api/v1/review/code-diff/requests/{request_id}",
    )


@router.get("/code-diff/requests/{request_id}")
async def get_code_review_request(request_id: UUID):
    """Poll the durable request state; completed verdicts come only from DB."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT request_id, job_id, project, status, verdict, score,
                   feedback, issues, flag_category, failure_stage,
                   needs_retry, model_used, error_detail, attempts,
                   created_at, started_at, completed_at, updated_at
            FROM code_review_requests
            WHERE request_id=$1
            """,
            request_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="리뷰 요청을 찾을 수 없습니다")
    result = dict(row)
    now = datetime.now(row["created_at"].tzinfo)
    queue_end = row["started_at"] or now
    run_end = row["completed_at"] or now
    result["measurements"] = {
        "schema": "review_pipeline.v1",
        "measurement_label": f"review.request.{row['status']}",
        "path": "async",
        "stage": "request",
        "outcome": row["status"],
        "queue_wait_ms": max(
            0, int((queue_end - row["created_at"]).total_seconds() * 1000)
        ),
        "run_ms": (
            max(0, int((run_end - row["started_at"]).total_seconds() * 1000))
            if row["started_at"] is not None else 0
        ),
        "total_ms": max(
            0, int((run_end - row["created_at"]).total_seconds() * 1000)
        ),
    }
    for key in ("request_id", "created_at", "started_at", "completed_at", "updated_at"):
        if result.get(key) is not None:
            result[key] = str(result[key])
    return result
