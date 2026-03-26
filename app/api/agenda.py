"""
CEO 아젠다 관리 API.
CEO와 프로젝트 CTO가 전략 논의/미결정 사항을 저장·추적·재개.

권한 규칙:
- CTO 세션: 자기 프로젝트 아젠다만 등록/조회/상태변경(논의중↔보류만). decide 불가.
- CEO 세션: 전체 프로젝트 CRUD + decide 가능.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.agenda_service import get_agenda_service

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── 요청/응답 모델 ─────────────────────────────────────────────────────────


class AgendaCreateRequest(BaseModel):
    project: str = Field(..., description="프로젝트 코드 (AADS, KIS, GO100, SF, NTV2, NAS)")
    title: str = Field(..., max_length=200, description="아젠다 제목")
    summary: str = Field(..., description="핵심 논점 + 옵션 + 미결정 사항 (마크다운)")
    priority: str = Field("P2", description="우선순위 P0~P3")
    tags: Optional[List[str]] = Field(None, description="검색용 태그")
    created_by: str = Field("CEO", description="CEO 또는 프로젝트명(CTO)")
    source_session_id: Optional[str] = Field(None, description="논의가 발생한 세션 ID")


class AgendaUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    summary: Optional[str] = None
    status: Optional[str] = Field(None, description="논의중, 보류, 결정, 진행중, 완료")
    priority: Optional[str] = Field(None, description="P0~P3")
    tags: Optional[List[str]] = None
    source_session_id: Optional[str] = None


class AgendaDecideRequest(BaseModel):
    decision: str = Field(..., description="CEO 결정 내용")


# ─── 엔드포인트 ──────────────────────────────────────────────────────────────


@router.post("/")
async def create_agenda(req: AgendaCreateRequest):
    """아젠다 등록."""
    svc = get_agenda_service()
    try:
        result = await svc.add_agenda(
            project=req.project,
            title=req.title,
            summary=req.summary,
            priority=req.priority,
            tags=req.tags,
            created_by=req.created_by,
            source_session_id=req.source_session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.get("/search")
async def search_agendas(keyword: str = Query(..., description="검색 키워드")):
    """title/summary/tags 키워드 검색."""
    svc = get_agenda_service()
    return await svc.search_agendas(keyword)


@router.get("/")
async def list_agendas(
    project: Optional[str] = Query(None, description="프로젝트 필터 (없으면 전체)"),
    status: Optional[str] = Query(None, description="상태 필터"),
    priority: Optional[str] = Query(None, description="우선순위 필터"),
):
    """아젠다 목록 조회. project=None이면 전체(CEO용), 지정 시 해당 프로젝트만(CTO용)."""
    svc = get_agenda_service()
    return await svc.list_agendas(project=project, status=status, priority=priority)


@router.get("/{agenda_id}")
async def get_agenda(agenda_id: int):
    """아젠다 단건 조회."""
    svc = get_agenda_service()
    result = await svc.get_agenda(agenda_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"아젠다 {agenda_id}를 찾을 수 없습니다.")
    return result


@router.patch("/{agenda_id}")
async def update_agenda(agenda_id: int, req: AgendaUpdateRequest):
    """아젠다 상태/내용 업데이트."""
    svc = get_agenda_service()
    try:
        result = await svc.update_agenda(
            agenda_id,
            title=req.title,
            summary=req.summary,
            status=req.status,
            priority=req.priority,
            tags=req.tags,
            source_session_id=req.source_session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail=f"아젠다 {agenda_id}를 찾을 수 없습니다.")
    return result


@router.post("/{agenda_id}/decide")
async def decide_agenda(agenda_id: int, req: AgendaDecideRequest):
    """CEO 결정 기록 — status='결정', decision 저장."""
    svc = get_agenda_service()
    result = await svc.decide_agenda(agenda_id, req.decision)
    if result is None:
        raise HTTPException(status_code=404, detail=f"아젠다 {agenda_id}를 찾을 수 없습니다.")
    return result
