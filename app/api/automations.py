"""업무자동화 API — 홈 배지용 승인 대기 건수 + 통합 목록·실행·일시정지·재개.

통합 목록·실행·일시정지·재개 4개 경로는 `AUTOMATION_DRAFTS_ENABLED` 가 꺼져 있으면
404 다(인증보다 먼저 확인). `/pending-count` 는 플래그와 무관하다.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import TenantRole, require_tenant_role
from app.services import automation_catalog as catalog
from app.services import automation_draft_flow as flow
from app.services import automation_draft_service as svc
from app.services.work_recipe.approval_notifier import pending_count

router = APIRouter(prefix="/automations", tags=["automations"])
TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)
require_member = require_tenant_role(TenantRole.MEMBER)


def _flag_on() -> None:
    if not svc.drafts_enabled():
        raise HTTPException(status_code=404, detail={"error": "automation_drafts_disabled"})


def _http(exc: flow.FlowError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"error": exc.code, **exc.extra})


def _actor(context: TenantContext) -> flow.Actor:
    try:
        return flow.actor_from_context(context)
    except flow.FlowError as exc:
        raise _http(exc) from None


async def _run(coro: Any) -> Any:
    try:
        return await coro
    except flow.FlowError as exc:
        raise _http(exc) from None
    except svc.DraftsDisabled:
        raise HTTPException(status_code=404, detail={"error": "automation_drafts_disabled"}) from None


@router.get("/pending-count")
async def get_pending_count(
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    """현재 테넌트의 승인 대기 건수와 가장 오래된 대기 시간(시간 단위)."""
    return await pending_count(tenant_id=str(context["tenant"]["id"]))


@router.get("", dependencies=[Depends(_flag_on)])
async def list_automations(
    status: Literal["available", "waiting", "broken", "expired"] | None = Query(default=None),
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    """내 자동화 통합 목록. 출처·소유자는 관리자 상세에서만 보인다."""
    actor = _actor(context)
    return await _run(catalog.list_automations(
        tenant_id=actor.tenant_id, user=actor.user, is_admin=actor.is_admin, status=status,
    ))


@router.post("/{key}/run", dependencies=[Depends(_flag_on)])
async def run_automation(key: str, context: TenantContext = Depends(require_member)) -> dict[str, Any]:
    return await _run(flow.run_now(_actor(context), context, key))


@router.post("/{key}/pause", dependencies=[Depends(_flag_on)])
async def pause_automation(key: str, context: TenantContext = Depends(require_member)) -> dict[str, Any]:
    return await _run(flow.pause_schedule(_actor(context), key))


@router.post("/{key}/resume", dependencies=[Depends(_flag_on)])
async def resume_automation(key: str, context: TenantContext = Depends(require_member)) -> dict[str, Any]:
    return await _run(flow.resume_schedule(_actor(context), key))
