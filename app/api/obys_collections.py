"""오비서 클로브 다회사 수집 API — 서버·데이터 계약(UI 는 이 계약을 읽기만 한다).

회사 발견·연결 승인·권한 확인·차단은 내부 관리자 전용이다(클로브 연결은 전역 한 개라 테넌트 소유가 아니다).
수집 실행·검토·확정은 연결된 테넌트의 활성 멤버십 역할로 나눈다: 수집 owner/admin, 검토 member 이상, 확정 owner/admin.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import get_current_user, require_internal_admin
from app.services import clobe_mcp_client as clobe
from app.services import obys_collection_service as svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/obys-collections", tags=["obys-collections"])

_READ_ROLES = {"owner", "admin", "member", "viewer"}
_REVIEW_ROLES = {"owner", "admin", "member"}
_MANAGE_ROLES = {"owner", "admin"}


def _role_ok(user: dict[str, Any], roles: set[str]) -> None:
    membership = user.get("current_membership") or {}
    same = str(membership.get("tenant_id") or "") == str(user.get("tenant_id") or "")
    role = str(membership.get("role") or "").strip().lower()
    status = str(membership.get("status") or "").strip().lower()
    if not same or status != "active" or role not in roles:
        raise HTTPException(status_code=403, detail="role_not_allowed")


def _actor(user: dict[str, Any]) -> str:
    return str(user.get("email") or user.get("user_id") or user.get("sub") or "unknown")[:320]


def _raise(exc: Exception) -> HTTPException:
    if isinstance(exc, svc.CollectionError):
        return HTTPException(status_code=exc.status, detail=exc.code)
    if isinstance(exc, clobe.ClobeReauthRequired):
        return HTTPException(status_code=409, detail="reauth_required")
    if isinstance(exc, clobe.ClobeTransientError):
        return HTTPException(status_code=503, detail="clobe_temporarily_unavailable")
    if isinstance(exc, clobe.ClobeToolDenied):
        return HTTPException(status_code=403, detail="tool_denied")
    return HTTPException(status_code=502, detail="clobe_error")


class LinkRequest(BaseModel):
    business_id: str = Field(min_length=3, max_length=64)
    name_evidence: bool = False


class RunRequest(BaseModel):
    kinds: list[Literal["bank_transaction", "tax_invoice", "cash_receipt", "card_approval"]] | None = None
    period_start: str | None = Field(None, max_length=10)
    period_end: str | None = Field(None, max_length=10)
    mode: Literal["live", "shadow"] = "live"


class ItemsRequest(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=500)


class ReviewRequest(ItemsRequest):
    decision: Literal["approve", "reject"]


@router.get("/status")
async def status(
    scope: Literal["tenant", "all"] = Query("tenant"),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """회사별 수집 상태·연결·권한·항목 단계 건수. scope=all 은 내부 관리자 전용."""
    try:
        if scope == "all":
            if not user.get("is_internal_admin"):
                raise HTTPException(status_code=403, detail="Internal admin access required")
            payload = await svc.company_status(None, internal_admin=True)
        else:
            _role_ok(user, _READ_ROLES)
            payload = await svc.company_status(user.get("tenant_id"))
        payload["auth"] = await _auth_view(user)
        return payload
    except (svc.CollectionError, clobe.ClobeError) as exc:
        raise _raise(exc) from None


async def _auth_view(user: dict[str, Any]) -> dict[str, Any]:
    """클로브 인증 상태. 토큰·만료 시각은 내부 관리자에게만, 나머지는 갱신 필요 여부만 보인다."""
    full = await clobe.get_status()
    view = {"status": full["status"], "reauth_required": full["reauth_required"],
            "last_success_at": full["last_success_at"]}
    if user.get("is_internal_admin"):
        view["token_expires_at"] = full["token_expires_at"]
        view["reauth_endpoint"] = "/api/v1/integrations/clobe/reauth"
    return view


@router.post("/admin/discover")
async def discover(_: dict[str, Any] = Depends(require_internal_admin)) -> dict[str, Any]:
    try:
        return await svc.discover_companies()
    except (svc.CollectionError, clobe.ClobeError) as exc:
        raise _raise(exc) from None


@router.post("/admin/companies/{company_id}/permission-check")
async def permission_check(company_id: str, _: dict[str, Any] = Depends(require_internal_admin)) -> dict[str, Any]:
    try:
        return await svc.check_company_permission(company_id)
    except (svc.CollectionError, clobe.ClobeError) as exc:
        raise _raise(exc) from None


@router.post("/admin/companies/{company_id}/link")
async def link_company(company_id: str, body: LinkRequest, admin: dict[str, Any] = Depends(require_internal_admin)) -> dict[str, Any]:
    try:
        return await svc.approve_link(company_id, body.business_id, actor=_actor(admin), name_evidence=body.name_evidence)
    except svc.CollectionError as exc:
        raise _raise(exc) from None


@router.post("/admin/companies/{company_id}/block")
async def block_company(company_id: str, admin: dict[str, Any] = Depends(require_internal_admin)) -> dict[str, Any]:
    try:
        return await svc.block_company(company_id, actor=_actor(admin))
    except svc.CollectionError as exc:
        raise _raise(exc) from None


@router.post("/companies/{company_id}/runs")
async def start_run(company_id: str, body: RunRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    try:
        if user.get("is_internal_admin"):
            await svc.require_runnable_company(company_id)
        else:
            _role_ok(user, _MANAGE_ROLES)
            await svc.require_company_tenant(company_id, user.get("tenant_id"))
        return await svc.run_collection(
            company_id, kinds=body.kinds, period_start=body.period_start, period_end=body.period_end, mode=body.mode)
    except (svc.CollectionError, clobe.ClobeError) as exc:
        raise _raise(exc) from None


@router.get("/companies/{company_id}/runs")
async def list_runs(company_id: str, limit: int = Query(20, ge=1, le=100),
                    user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    try:
        admin = bool(user.get("is_internal_admin"))
        if not admin:
            _role_ok(user, _READ_ROLES)
        return {"runs": await svc.list_runs(user.get("tenant_id"), company_id, limit=limit, internal_admin=admin)}
    except svc.CollectionError as exc:
        raise _raise(exc) from None


@router.get("/businesses/{business_id}/items")
async def items(
    business_id: str,
    stage: Literal["review_box", "reviewed", "confirmed", "rejected", "superseded"] | None = None,
    kind: Literal["bank_transaction", "tax_invoice", "cash_receipt", "card_approval"] | None = None,
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    _role_ok(user, _READ_ROLES)
    try:
        return await svc.list_items(user.get("tenant_id"), business_id, stage=stage, kind=kind, limit=limit, offset=offset)
    except svc.CollectionError as exc:
        raise _raise(exc) from None


@router.post("/businesses/{business_id}/items/review")
async def review(business_id: str, body: ReviewRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    _role_ok(user, _REVIEW_ROLES)
    try:
        return await svc.review_items(user.get("tenant_id"), business_id, body.item_ids, body.decision, actor=_actor(user))
    except svc.CollectionError as exc:
        raise _raise(exc) from None


@router.post("/businesses/{business_id}/items/confirm")
async def confirm(business_id: str, body: ItemsRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    _role_ok(user, _MANAGE_ROLES)
    try:
        return await svc.confirm_items(user.get("tenant_id"), business_id, body.item_ids, actor=_actor(user))
    except svc.CollectionError as exc:
        raise _raise(exc) from None
