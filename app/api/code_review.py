"""
AI-to-AI 피드백 시스템 — Code Review API 엔드포인트.
Pipeline Runner(bash)가 curl로 호출.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
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
