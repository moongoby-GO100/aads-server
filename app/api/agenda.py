"""
CEO 아젠다 관리 API.
CEO와 프로젝트 CTO가 전략 논의/미결정 사항을 저장·추적·재개.

권한 규칙:
- CTO 세션: 자기 프로젝트 아젠다만 등록/조회/상태변경(논의중↔보류만). decide 불가.
- CEO 세션: 전체 프로젝트 CRUD + decide 가능.
"""
from __future__ import annotations

import structlog
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.agenda_service import get_agenda_service

logger = structlog.get_logger(__name__)
router = APIRouter()


# ─── 요청/응답 모델 ─────────────────────────────────────────────────────────


class AgendaCreateRequest(BaseModel):
    project: str = Field(..., description="프로젝트 코드 (AADS, KIS, GO100, SF, NTV2, NAS)")
    title: str = Field(..., max_length=200, description="아젠다 제목")
    summary: Optional[str] = Field(None, description="핵심 논점 + 옵션 + 미결정 사항 (마크다운)")
    priority: str = Field("P2", description="우선순위 P0~P3")
    tags: Optional[List[str]] = Field(None, description="검색용 태그")
    created_by: str = Field("CEO", description="CEO 또는 프로젝트명(CTO)")
    source_session_id: Optional[str] = Field(None, description="논의가 발생한 세션 ID")
    related_task_id: Optional[str] = Field(None, description="연결된 지시서 ID")


class AgendaUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    summary: Optional[str] = None
    status: Optional[str] = Field(None, description="논의중, 보류, 결정, 진행중, 완료, 폐기")
    priority: Optional[str] = Field(None, description="P0~P3")
    tags: Optional[List[str]] = None
    source_session_id: Optional[str] = None
    related_task_id: Optional[str] = None
    caller_role: str = Field("CEO", description="호출자 역할 (CEO/CTO)")
    caller_project: Optional[str] = Field(None, description="CTO의 담당 프로젝트")


class AgendaDecideRequest(BaseModel):
    decision: str = Field(..., description="CEO 결정 내용")
    decided_by: str = Field("CEO", description="결정자")
    caller_role: str = Field("CEO", description="호출자 역할 — CEO만 허용")


# ─── 엔드포인트 ──────────────────────────────────────────────────────────────


@router.post("/", operation_id="add_agenda", summary="아젠다 등록")
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
            related_task_id=req.related_task_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.get("/search", operation_id="search_agendas", summary="아젠다 텍스트 검색")
async def search_agendas(
    keyword: str = Query(..., description="검색 키워드"),
    project: Optional[str] = Query(None, description="프로젝트 필터"),
    limit: int = Query(20, ge=1, le=100),
):
    """title/summary/tags 키워드 검색."""
    svc = get_agenda_service()
    items = await svc.search_agendas(keyword=keyword, project=project, limit=limit)
    return {"query": keyword, "count": len(items), "items": items}


@router.get("/sessions", operation_id="list_agenda_sessions", summary="아젠다 세션 목록 조회")
async def list_agenda_sessions(
    project: Optional[str] = Query(None, description="프로젝트 필터 (없으면 전체)"),
):
    """아젠다에 연결된 고유 세션 ID 목록 반환."""
    svc = get_agenda_service()
    sessions = await svc.list_sessions(project=project)
    return {"sessions": sessions}


@router.get("/", operation_id="list_agendas", summary="아젠다 목록 조회")
async def list_agendas(
    project: Optional[str] = Query(None, description="프로젝트 필터 (없으면 전체)"),
    status: Optional[str] = Query(None, description="상태 필터"),
    priority: Optional[str] = Query(None, description="우선순위 필터"),
    created_by: Optional[str] = Query(None, description="등록자 필터"),
    source_session_id: Optional[str] = Query(None, description="세션 ID 필터"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """아젠다 목록 조회. project=None이면 전체(CEO용), 지정 시 해당 프로젝트만(CTO용)."""
    svc = get_agenda_service()
    return await svc.list_agendas(
        project=project,
        status=status,
        priority=priority,
        created_by=created_by,
        source_session_id=source_session_id,
        limit=limit,
        offset=offset,
    )


@router.get("/{agenda_id}", operation_id="get_agenda", summary="아젠다 단건 조회")
async def get_agenda(agenda_id: int):
    """아젠다 단건 조회."""
    svc = get_agenda_service()
    result = await svc.get_agenda(agenda_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"아젠다 {agenda_id}를 찾을 수 없습니다.")
    return result


@router.patch("/{agenda_id}", operation_id="update_agenda", summary="아젠다 수정")
async def update_agenda(agenda_id: int, req: AgendaUpdateRequest):
    """아젠다 상태/내용 업데이트.
    - CEO: 모든 필드 수정 가능
    - CTO: 자기 프로젝트만, 논의중↔보류 전환만
    """
    svc = get_agenda_service()
    try:
        result = await svc.update_agenda(
            agenda_id,
            caller_role=req.caller_role,
            caller_project=req.caller_project,
            title=req.title,
            summary=req.summary,
            status=req.status,
            priority=req.priority,
            tags=req.tags,
            source_session_id=req.source_session_id,
            related_task_id=req.related_task_id,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail=f"아젠다 {agenda_id}를 찾을 수 없습니다.")
    return result


@router.post("/{agenda_id}/decide", operation_id="decide_agenda", summary="CEO 결정 등록")
async def decide_agenda(agenda_id: int, req: AgendaDecideRequest):
    """CEO 결정 기록 — status='결정', decision 저장. CEO만 가능."""
    svc = get_agenda_service()
    try:
        result = await svc.decide_agenda(
            agenda_id,
            decision=req.decision,
            decided_by=req.decided_by,
            caller_role=req.caller_role,
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail=f"아젠다 {agenda_id}를 찾을 수 없습니다.")
    return result
