"""API routes for the Yeoljeong store-assistant approvals / notifications / audit-log center.

Not registered in main.py yet (per task spec) — wire up with:
    from app.api import yeoljeong_ops
    app.include_router(yeoljeong_ops.router, prefix="/api/v1")
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.services import yeoljeong_ops_service as svc

router = APIRouter(prefix="/yeoljeong-ops", tags=["yeoljeong-ops"])
logger = logging.getLogger(__name__)


def _actor_of(current_user: dict[str, Any]) -> str:
    return str(current_user.get("email") or current_user.get("user_id") or "unknown")


class DecisionPayload(BaseModel):
    memo: str = ""


class ReadAllPayload(BaseModel):
    business_id: str = "biz-mia"
    target_user: str = ""


class LogActionPayload(BaseModel):
    business_id: str = "biz-mia"
    actor: str = ""
    action: str
    resource_type: str
    resource_id: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    ip_address: str = ""


class CreateApprovalPayload(BaseModel):
    business_id: str = "biz-mia"
    approval_type: str
    reference_id: str
    title: str
    description: str = ""
    requested_by: str = ""
    priority: str = "normal"


class NotifyPayload(BaseModel):
    business_id: str = "biz-mia"
    target_user: str = ""
    notification_type: str
    title: str
    body: str = ""
    reference_type: str = ""
    reference_id: str = ""


# ---------------------------------------------------------------------------
# 승인함 (/approvals)
# ---------------------------------------------------------------------------
@router.get("/approvals")
async def list_approvals(
    status: str | None = None,
    approval_type: str | None = None,
    business_id: str = "biz-mia",
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    rows = await svc.list_approvals(business_id=business_id, status=status, approval_type=approval_type)
    return {"approvals": rows, "count": len(rows)}


@router.get("/approvals/count")
async def approvals_count(
    business_id: str = "biz-mia",
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    count = await svc.count_pending_approvals(business_id=business_id)
    return {"pending": count}


@router.post("/approvals/{approval_id}/approve")
async def approve_approval_endpoint(
    approval_id: str,
    payload: DecisionPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    result = await svc.approve_approval(approval_id, payload.memo, _actor_of(current_user))
    if not result:
        raise HTTPException(status_code=404, detail="approval not found")
    return {"approval": result}


@router.post("/approvals/{approval_id}/reject")
async def reject_approval_endpoint(
    approval_id: str,
    payload: DecisionPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    result = await svc.reject_approval(approval_id, payload.memo, _actor_of(current_user))
    if not result:
        raise HTTPException(status_code=404, detail="approval not found")
    return {"approval": result}


# ---------------------------------------------------------------------------
# 알림센터 (/notifications)
# ---------------------------------------------------------------------------
@router.get("/notifications")
async def list_notifications(
    is_read: bool | None = None,
    limit: int = 50,
    business_id: str = "biz-mia",
    target_user: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    rows = await svc.list_notifications(
        business_id=business_id, target_user=target_user, is_read=is_read, limit=limit
    )
    return {"notifications": rows, "count": len(rows)}


@router.get("/notifications/unread-count")
async def notifications_unread_count(
    business_id: str = "biz-mia",
    target_user: str | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    count = await svc.count_unread_notifications(business_id=business_id, target_user=target_user)
    return {"unread": count}


@router.post("/notifications/{notification_id}/read")
async def read_notification(
    notification_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    result = await svc.mark_notification_read(notification_id)
    if not result:
        raise HTTPException(status_code=404, detail="notification not found")
    return {"notification": result}


@router.post("/notifications/read-all")
async def read_all_notifications(
    payload: ReadAllPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    count = await svc.mark_all_notifications_read(
        business_id=payload.business_id,
        target_user=payload.target_user or None,
    )
    return {"updated": count}


# ---------------------------------------------------------------------------
# 감사로그 (/audit-logs)
# ---------------------------------------------------------------------------
@router.get("/audit-logs")
async def list_audit_logs(
    actor: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    business_id: str = "biz-mia",
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    rows = await svc.list_audit_logs(
        business_id=business_id,
        actor=actor,
        action=action,
        resource_type=resource_type,
        date_from=date_from,
        date_to=date_to,
    )
    return {"audit_logs": rows, "count": len(rows)}


@router.get("/audit-logs/summary")
async def audit_logs_summary(
    date_from: str | None = None,
    date_to: str | None = None,
    business_id: str = "biz-mia",
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    return await svc.audit_log_summary(business_id=business_id, date_from=date_from, date_to=date_to)


# ---------------------------------------------------------------------------
# 내부 유틸리티 (/internal) — 다른 서비스에서 호출용
# ---------------------------------------------------------------------------
@router.post("/internal/log-action")
async def internal_log_action(
    payload: LogActionPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    actor = payload.actor or _actor_of(current_user)
    result = await svc.log_action(
        business_id=payload.business_id,
        actor=actor,
        action=payload.action,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        details=payload.details,
        ip_address=payload.ip_address,
    )
    return {"audit_log": result}


@router.post("/internal/create-approval")
async def internal_create_approval(
    payload: CreateApprovalPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    result = await svc.create_approval(
        business_id=payload.business_id,
        approval_type=payload.approval_type,
        reference_id=payload.reference_id,
        title=payload.title,
        description=payload.description,
        requested_by=payload.requested_by or _actor_of(current_user),
        priority=payload.priority,
    )
    return {"approval": result}


@router.post("/internal/notify")
async def internal_notify(
    payload: NotifyPayload,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    result = await svc.create_notification(
        business_id=payload.business_id,
        target_user=payload.target_user,
        notification_type=payload.notification_type,
        title=payload.title,
        body=payload.body,
        reference_type=payload.reference_type,
        reference_id=payload.reference_id,
    )
    return {"notification": result}
