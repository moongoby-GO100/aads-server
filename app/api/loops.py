"""OHVIS Loop API — Phase 1 REST 엔드포인트.

기획서: docs/AADS-LAYOUT-001_OHVIS-LOOP-SYSTEM.md §11
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.loop_controller import (
    VALID_LOOP_TYPES,
    create_loop,
    get_loop,
    list_active_loops,
    pause_loop,
    resume_loop,
    cancel_loop,
    check_safety_limits,
)

logger = logging.getLogger("ohvis.loops_api")
router = APIRouter()


class LoopCreateRequest(BaseModel):
    loop_type: str = Field(..., description="monitor | task | sequential")
    command: str = Field(..., description="CEO 원문 지시")
    project: str = Field("AADS", description="대상 프로젝트")
    model_id: Optional[str] = Field(None, description="실행 모델 ID")
    interval_seconds: Optional[int] = Field(None, description="반복 간격 (초)")
    max_iterations: Optional[int] = Field(None, description="최대 반복 횟수")
    max_cost_override: Optional[float] = Field(None, description="CEO 비용 상한 수동 지정")
    max_failures: Optional[int] = Field(None, description="최대 연속 실패 횟수")
    timeout_minutes: Optional[int] = Field(None, description="제한 시간 (분)")
    success_condition: Optional[dict] = Field(None, description="성공 조건 JSON")
    alert_condition: Optional[dict] = Field(None, description="알림 조건 JSON")
    session_id: Optional[str] = Field(None, description="채팅 세션 ID")
    task_list: Optional[list] = Field(None, description="Sequential 루프 작업 목록")


class LoopStatusResponse(BaseModel):
    id: int
    loop_type: str
    status: str
    current_iteration: Optional[int] = 0
    total_cost_usd: Optional[float] = 0.0
    max_cost_usd: Optional[float] = None
    max_iterations: Optional[int] = None
    execution_model_id: Optional[str] = None
    project: Optional[str] = None


@router.post("/loops", summary="루프 생성")
async def api_create_loop(req: LoopCreateRequest):
    if req.loop_type not in VALID_LOOP_TYPES:
        raise HTTPException(400, f"Invalid loop_type: {req.loop_type}. Must be one of {VALID_LOOP_TYPES}")

    result = await create_loop(
        loop_type=req.loop_type,
        original_command=req.command,
        project=req.project,
        interval_seconds=req.interval_seconds,
        max_iterations=req.max_iterations,
        max_cost_override=req.max_cost_override,
        max_failures=req.max_failures,
        timeout_minutes=req.timeout_minutes,
        success_condition=req.success_condition,
        alert_condition=req.alert_condition,
        execution_model_id=req.model_id,
        session_id=req.session_id,
        task_list=req.task_list,
    )
    logger.info("Loop created via API: %s", result)
    return {"ok": True, "loop": result}


@router.get("/loops", summary="활성 루프 목록")
async def api_list_loops(
    project: Optional[str] = Query(None, description="프로젝트 필터"),
):
    loops = await list_active_loops(project)
    return {"ok": True, "count": len(loops), "loops": _serialize_list(loops)}


@router.get("/loops/{loop_id}", summary="루프 상세 조회")
async def api_get_loop(loop_id: int):
    loop = await get_loop(loop_id)
    if not loop:
        raise HTTPException(404, f"Loop #{loop_id} not found")
    return {"ok": True, "loop": _serialize(loop)}


@router.get("/loops/{loop_id}/safety", summary="안전 제한 체크")
async def api_check_safety(loop_id: int):
    result = await check_safety_limits(loop_id)
    return {"ok": result["ok"], "detail": result}


@router.post("/loops/{loop_id}/pause", summary="루프 일시정지")
async def api_pause_loop(loop_id: int, reason: str = Query("CEO 요청")):
    result = await pause_loop(loop_id, reason)
    if not result:
        raise HTTPException(404, f"Loop #{loop_id} not found or not active")
    return {"ok": True, "loop": result}


@router.post("/loops/{loop_id}/resume", summary="루프 재개")
async def api_resume_loop(loop_id: int):
    result = await resume_loop(loop_id)
    if not result:
        raise HTTPException(404, f"Loop #{loop_id} not found or not paused")
    return {"ok": True, "loop": result}


@router.post("/loops/{loop_id}/cancel", summary="루프 취소")
async def api_cancel_loop(loop_id: int, reason: str = Query("CEO 중단 명령")):
    result = await cancel_loop(loop_id, reason)
    if not result:
        raise HTTPException(404, f"Loop #{loop_id} not found")
    return {"ok": True, "loop": result}


def _serialize(row: dict) -> dict:
    from datetime import datetime
    from decimal import Decimal
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _serialize_list(rows: list[dict]) -> list[dict]:
    return [_serialize(r) for r in rows]
