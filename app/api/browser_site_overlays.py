"""사이트별 공지 팝업 공용 목록 관리 API — 읽기·후보 등록·상태 변경 최소.

후보 등록은 항상 status=candidate 다. 승인·반려·비활성은 레시피 등록 승인과 같은 검사
(내부 관리자 + 사람 주체, 승인자는 인증 주체로만 결정)를 거친다. 대시보드 화면은 후속 작업이다.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.ohvis_console import TenantContext, require_console_admin
from app.services.work_recipe import site_overlays
from app.services.work_recipe.registration import decider_identity, is_human_principal

router = APIRouter(prefix="/ohvis/site-overlays", tags=["browser-site-overlays"])

_ERROR_STATUS = {"overlay_not_found": 404, "overlay_exists": 409, "invalid_transition": 409}


class SiteOverlayCandidateIn(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    root_selector: str = Field(min_length=1, max_length=300)
    close_selector: str = Field(min_length=1, max_length=300)
    label: str = Field(default="", max_length=site_overlays.LABEL_MAX_LEN)
    source: str = Field(default="manual", max_length=20, description="manual | recorded")
    evidence: dict[str, Any] = Field(default_factory=dict, description="screenshot_url · page_url · note 만, 비밀값 금지")


class SiteOverlayDecisionIn(BaseModel):
    status: str = Field(min_length=1, max_length=20, description="approved | rejected | disabled")


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _http_error(exc: site_overlays.SiteOverlayError) -> HTTPException:
    return HTTPException(status_code=_ERROR_STATUS.get(exc.code, 422), detail=exc.code if exc.code in _ERROR_STATUS else str(exc))


@router.get("")
async def list_site_overlays(
    status: str | None = Query(default=None),
    domain: str | None = Query(default=None, max_length=253),
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    try:
        rows = await site_overlays.list_overlays(tenant_id=_tenant_id(context), status=status, domain=domain)
    except site_overlays.SiteOverlayError as exc:
        raise _http_error(exc) from exc
    return {"overlays": rows, "count": len(rows)}


@router.post("")
async def register_site_overlay_candidate(
    body: SiteOverlayCandidateIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    try:
        row = await site_overlays.add_candidate(
            tenant_id=_tenant_id(context),
            domain=body.domain,
            root_selector=body.root_selector,
            close_selector=body.close_selector,
            label=body.label,
            source=body.source,
            evidence=body.evidence,
        )
    except site_overlays.SiteOverlayError as exc:
        raise _http_error(exc) from exc
    return {"status": row["status"], "overlay": row}


@router.post("/{overlay_id}/decision")
async def decide_site_overlay(
    overlay_id: str,
    body: SiteOverlayDecisionIn,
    context: TenantContext = Depends(require_console_admin),
) -> dict[str, Any]:
    if not is_human_principal(context):
        raise HTTPException(status_code=403, detail="human_decider_required")
    try:
        row = await site_overlays.decide_overlay(
            overlay_id=overlay_id,
            tenant_id=_tenant_id(context),
            decision=body.status,
            decided_by=decider_identity(context),
        )
    except site_overlays.SiteOverlayError as exc:
        raise _http_error(exc) from exc
    return {"status": row["status"], "overlay": row}
