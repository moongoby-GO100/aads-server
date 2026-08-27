"""OHVIS managed browser task API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.services.browser_task_gateway import (
    consume_approval_token,
    create_browser_task,
    decide_permission,
    get_browser_task,
    list_browser_tasks,
    list_permission_requests,
    request_task_permission,
    update_browser_task_status,
)
from app.services.managed_browser import profile_info

router = APIRouter(prefix="/browser-tasks", tags=["browser-tasks"])
TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)
require_member = require_tenant_role(TenantRole.MEMBER)


class BrowserTaskCreate(BaseModel):
    work_key: str = Field(min_length=1, max_length=120)
    target_url: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    current_step: str = Field(default="", max_length=500)


class BrowserTaskStatusPatch(BaseModel):
    status: str = Field(min_length=1, max_length=80)
    current_step: str = Field(default="", max_length=500)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = Field(default="", max_length=1000)


class PermissionRequestIn(BaseModel):
    work_key: str = Field(min_length=1, max_length=120)
    origin: str = Field(default="", max_length=500)
    action_type: str = Field(min_length=1, max_length=120)
    action_summary: str = Field(default="", max_length=1000)
    payload: dict[str, Any] = Field(default_factory=dict)
    automation_scope: dict[str, Any] = Field(default_factory=dict)
    max_executions: int = Field(default=1, ge=1, le=500)


class PermissionDecisionIn(BaseModel):
    reason: str = Field(default="", max_length=1000)
    approval_scope: dict[str, Any] = Field(default_factory=dict)
    max_executions: int | None = Field(default=None, ge=1, le=500)


class ApprovalTokenConsumeIn(BaseModel):
    approval_token: str = Field(min_length=20, max_length=300, json_schema_extra={"writeOnly": True})
    action_type: str = Field(min_length=1, max_length=120)
    origin: str = Field(default="", max_length=500)
    selector: str = Field(default="", max_length=500)
    payload: dict[str, Any] = Field(default_factory=dict)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _user_id(context: TenantContext) -> str:
    return str(context["membership"]["user_id"])


def _session_id_from_request(request: Request) -> str | None:
    for key in ("x-aads-chat-session-id", "x-chat-session-id"):
        value = request.headers.get(key, "").strip()
        if value:
            return value
    value = request.query_params.get("session_id", "").strip()
    return value or None


@router.get("")
async def api_list_browser_tasks(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    tasks = await list_browser_tasks(tenant_id=_tenant_id(context), status=status, limit=limit)
    return {"tasks": tasks, "count": len(tasks)}


@router.post("")
async def api_create_browser_task(
    body: BrowserTaskCreate,
    request: Request,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    session_id = body.session_id or _session_id_from_request(request)
    task = await create_browser_task(
        tenant_id=_tenant_id(context),
        user_id=_user_id(context),
        work_key=body.work_key,
        target_url=body.target_url,
        session_id=session_id,
        current_step=body.current_step,
    )
    return {"status": "created", "task": task, "profile": profile_info(body.work_key, body.target_url)}


@router.get("/{task_id}")
async def api_get_browser_task(
    task_id: str,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    task = await get_browser_task(tenant_id=_tenant_id(context), task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    return task


@router.patch("/{task_id}/status")
async def api_update_browser_task_status(
    task_id: str,
    body: BrowserTaskStatusPatch,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    task = await update_browser_task_status(
        tenant_id=_tenant_id(context),
        task_id=task_id,
        status=body.status,
        current_step=body.current_step,
        result=body.result,
        error=body.error,
    )
    if not task:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    return {"status": "updated", "task": task}


@router.post("/{task_id}/permissions")
async def api_request_permission(
    task_id: str,
    body: PermissionRequestIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    task = await get_browser_task(tenant_id=_tenant_id(context), task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    return await request_task_permission(
        tenant_id=_tenant_id(context),
        task_id=task_id,
        work_key=body.work_key,
        origin=body.origin,
        action_type=body.action_type,
        action_summary=body.action_summary,
        requested_by=_user_id(context),
        payload=body.payload,
        automation_scope=body.automation_scope,
        max_executions=body.max_executions,
    )


@router.get("/permissions/pending")
async def api_list_permissions(
    decision: str = "pending",
    limit: int = Query(default=50, ge=1, le=200),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    requests = await list_permission_requests(tenant_id=_tenant_id(context), decision=decision, limit=limit)
    return {"requests": requests, "count": len(requests)}


@router.post("/permissions/{request_id}/approve")
async def api_approve_permission(
    request_id: str,
    body: PermissionDecisionIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    request = await decide_permission(
        tenant_id=_tenant_id(context),
        request_id=request_id,
        decision="approved",
        decided_by=_user_id(context),
        reason=body.reason,
        approval_scope=body.approval_scope,
        max_executions=body.max_executions,
    )
    if not request:
        raise HTTPException(status_code=404, detail="permission_request_not_found_or_expired")
    return {"status": "approved", "request": request}


@router.post("/{task_id}/approval-token/consume")
async def api_consume_approval_token(
    task_id: str,
    body: ApprovalTokenConsumeIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    task = await get_browser_task(tenant_id=_tenant_id(context), task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    result = await consume_approval_token(
        tenant_id=_tenant_id(context),
        task_id=task_id,
        approval_token=body.approval_token,
        action_type=body.action_type,
        origin=body.origin,
        selector=body.selector,
        payload=body.payload,
    )
    if result.get("status") != "approved":
        raise HTTPException(status_code=403, detail=result)
    return result


@router.post("/permissions/{request_id}/reject")
async def api_reject_permission(
    request_id: str,
    body: PermissionDecisionIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    request = await decide_permission(
        tenant_id=_tenant_id(context),
        request_id=request_id,
        decision="rejected",
        decided_by=_user_id(context),
        reason=body.reason,
    )
    if not request:
        raise HTTPException(status_code=404, detail="permission_request_not_found_or_expired")
    return {"status": "rejected", "request": request}
