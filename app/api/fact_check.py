"""
AADS 팩트 체크 API
POST /api/v1/fact-check/verify        — 단일 주장 검증
POST /api/v1/fact-check/verify-batch  — 다중 주장 일괄 검증
"""
from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.fact_checker import get_fact_checker
from app.core.db_pool import get_pool

router = APIRouter()


class VerifyRequest(BaseModel):
    claim: str
    session_id: Optional[str] = None


class BatchVerifyRequest(BaseModel):
    claims: List[str]
    session_id: Optional[str] = None


@router.post("/verify")
async def verify_claim(req: VerifyRequest):
    """단일 주장 팩트 체크 — DB + 웹 교차 검증."""
    if not req.claim.strip():
        raise HTTPException(status_code=400, detail="검증할 주장을 입력하세요")
    try:
        pool = get_pool()
        checker = get_fact_checker(pool)
        result = await checker.check(req.claim.strip(), req.session_id)
        return result.to_dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"팩트 체크 실패: {e}")


@router.post("/verify-batch")
async def verify_batch(req: BatchVerifyRequest):
    """다중 주장 일괄 팩트 체크."""
    if not req.claims:
        raise HTTPException(status_code=400, detail="검증할 주장 목록을 입력하세요")
    if len(req.claims) > 10:
        raise HTTPException(status_code=400, detail="한 번에 최대 10개까지 검증 가능합니다")
    try:
        pool = get_pool()
        checker = get_fact_checker(pool)
        results = await checker.check_multiple(req.claims, req.session_id)
        return [r.to_dict() for r in results]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"일괄 팩트 체크 실패: {e}")
