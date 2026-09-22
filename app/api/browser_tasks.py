"""OHVIS managed browser task API."""
# ruff: noqa: B008  # FastAPI dependency injection uses module-level dependencies.
from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role, verify_token
from app.services.browser_task_gateway import (
    capture_browser_task_live_frame,
    check_browser_target_access,
    consume_approval_token,
    create_browser_task,
    decide_permission,
    get_browser_task,
    get_browser_task_live_frame,
    list_browser_task_events,
    list_browser_task_steps,
    list_browser_tasks,
    list_permission_requests,
    record_browser_task_step,
    request_task_permission,
    update_browser_task_status,
    upsert_browser_task_live_frame,
)
from app.services.channel_router import (
    ChannelRouter,
    ObservationEnvelope,
    directive_from_authenticated_context,
    payload_hash,
)
from app.services.managed_browser import profile_info

router = APIRouter(prefix="/browser-tasks", tags=["browser-tasks"])
TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)
require_member = require_tenant_role(TenantRole.MEMBER)


def _websocket_token(websocket: WebSocket) -> str:
    for key in ("access_token", "auth_token", "token"):
        value = str(websocket.query_params.get(key) or "").strip()
        if value:
            return value
    return str((websocket.cookies or {}).get("aads_token") or "").strip()


def _websocket_principal(websocket: WebSocket) -> dict[str, Any]:
    token = _websocket_token(websocket)
    payload = verify_token(token) if token else None
    if not payload:
        return {}
    return {
        "user_id": str(payload.get("sub") or "").strip(),
        "tenant_id": str(payload.get("tenant_id") or "").strip(),
        "is_admin": bool(payload.get("is_admin")),
    }


class BrowserTaskCreate(BaseModel):
    work_key: str = Field(min_length=1, max_length=120)
    target_url: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    current_step: str = Field(default="", max_length=500)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=200)


class BrowserAccessCheckIn(BaseModel):
    work_key: str = Field(default="access-check", min_length=1, max_length=120)
    target_url: str = Field(min_length=1, max_length=2000)


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


class BrowserLiveFrameIn(BaseModel):
    frame_base64: str = Field(default="", max_length=2_500_000, json_schema_extra={"writeOnly": True})
    frame_url: str = Field(default="", max_length=2000)
    media_type: str = Field(default="image/jpeg", max_length=80)
    width: int | None = Field(default=None, ge=1, le=10000)
    height: int | None = Field(default=None, ge=1, le=10000)
    current_url: str = Field(default="", max_length=2000)
    page_title: str = Field(default="", max_length=500)
    current_step: str = Field(default="", max_length=500)
    cursor: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=200)


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


async def _display(payload: dict[str, Any], context: TenantContext) -> dict[str, Any]:
    from app.services.live_fact_gate import guard_payload_for_display

    return await guard_payload_for_display(payload, tenant_id=_tenant_id(context))


@router.get("")
async def api_list_browser_tasks(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    tasks = await list_browser_tasks(tenant_id=_tenant_id(context), status=status, limit=limit)
    displayed = [await _display(task, context) for task in tasks]
    return {"tasks": displayed, "count": len(displayed)}


@router.post("")
async def api_create_browser_task(
    body: BrowserTaskCreate,
    request: Request,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    session_id = body.session_id or _session_id_from_request(request)
    if not session_id:
        raise HTTPException(status_code=422, detail="MISSING_SESSION_ID")
    directive_payload = {
        "work_key": body.work_key,
        "target_url": body.target_url,
        "current_step": body.current_step,
    }
    correlation_id = body.correlation_id or f"browser-create:{session_id}"
    intent = ChannelRouter().route_directive(
        directive_from_authenticated_context(
            context,
            session_id=session_id,
            correlation_id=correlation_id,
            payload=directive_payload,
            capabilities=frozenset({"browser.task.create"}),
        ),
        capability="browser.task.create",
    )
    task = await create_browser_task(
        tenant_id=_tenant_id(context),
        user_id=_user_id(context),
        work_key=body.work_key,
        target_url=body.target_url,
        session_id=session_id,
        current_step=body.current_step,
        channel_audit={
            "decision": "accepted",
            "reason_code": "TRUSTED_COMMAND_CHANNEL",
            "source": intent.source,
            "correlation_id": intent.correlation_id,
            "capability": "browser.task.create",
            "payload_hash": intent.payload_hash,
        },
    )
    return {
        "status": "created" if task.get("id") else "creation_failed",
        "task": task,
        "profile": profile_info(body.work_key, body.target_url),
    }


@router.post("/access-check")
async def api_check_browser_target_access(
    body: BrowserAccessCheckIn,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    return await check_browser_target_access(work_key=body.work_key, target_url=body.target_url)


@router.get("/{task_id}")
async def api_get_browser_task(
    task_id: str,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    task = await get_browser_task(tenant_id=_tenant_id(context), task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    return await _display(task, context)


@router.get("/{task_id}/events")
async def api_list_browser_task_events(
    task_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    task = await get_browser_task(tenant_id=_tenant_id(context), task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    events = await list_browser_task_events(tenant_id=_tenant_id(context), task_id=task_id, limit=limit)
    displayed = [await _display(event, context) for event in events]
    return {"events": displayed, "count": len(displayed)}


@router.get("/{task_id}/steps")
async def api_list_browser_task_steps(
    task_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    task = await get_browser_task(tenant_id=_tenant_id(context), task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    steps = await list_browser_task_steps(tenant_id=_tenant_id(context), task_id=task_id, limit=limit)
    return {"steps": steps, "count": len(steps)}


@router.get("/{task_id}/live-frame")
async def api_get_browser_task_live_frame(
    task_id: str,
    event_limit: int = Query(default=20, ge=0, le=100),
    capture: bool = Query(default=False),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    task = await get_browser_task(tenant_id=_tenant_id(context), task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    capture_result: dict[str, Any] = {"status": "skipped", "reason": "capture_disabled"}
    if capture:
        capture_result = await capture_browser_task_live_frame(tenant_id=_tenant_id(context), task_id=task_id)
    frame = await get_browser_task_live_frame(tenant_id=_tenant_id(context), task_id=task_id)
    events = []
    if event_limit:
        events = await list_browser_task_events(tenant_id=_tenant_id(context), task_id=task_id, limit=event_limit)
    from app.services.browser_artifact_state import build_browser_artifact_status

    artifact_status = build_browser_artifact_status(task=task, frame=frame, events=events)
    return await _display(
        {"task": task, "frame": frame, "events": events, "capture": capture_result,
         "artifact_status": artifact_status},
        context,
    )


@router.websocket("/{task_id}/live-stream")
async def api_browser_task_live_stream(websocket: WebSocket, task_id: str) -> None:
    """Stream a server Chromium tab and accept operator CDP input on one socket."""
    principal = _websocket_principal(websocket)
    tenant_id = str(principal.get("tenant_id") or "")
    user_id = str(principal.get("user_id") or "")
    if not tenant_id or not user_id:
        await websocket.close(code=4401, reason="authentication_required")
        return
    try:
        task = await get_browser_task(tenant_id=tenant_id, task_id=task_id)
    except (ValueError, TypeError):
        task = None
    if not task:
        await websocket.close(code=4404, reason="browser_task_not_found")
        return
    task_user_id = str(task.get("user_id") or "").strip()
    if task_user_id and task_user_id != user_id and not principal.get("is_admin"):
        await websocket.close(code=4403, reason="forbidden")
        return

    from app.services.browser_live_control import BrowserLiveControlError, ServerBrowserLiveSession

    await websocket.accept()
    live = ServerBrowserLiveSession(target_url=str(task.get("target_url") or ""))
    try:
        state = await live.start()
        await record_browser_task_step(
            tenant_id=tenant_id,
            task_id=task_id,
            step="action",
            narration="서버 브라우저 실시간 화면을 연결했습니다.",
            guide="프레임을 클릭하거나 입력 도구로 직접 조작할 수 있습니다.",
            route="server_cdp_screencast",
            extra={"action": "live_stream_started", "runtime": "server_cdp"},
        )
        await websocket.send_json({"type": "ready", **state, "width": live.width, "height": live.height})

        async def send_frames() -> None:
            last_persisted = 0.0
            while True:
                try:
                    frame = await live.next_frame(timeout=15.0)
                except TimeoutError:
                    await websocket.send_json({"type": "heartbeat"})
                    continue
                state_now = await live.page_state()
                frame.update(state_now)
                await websocket.send_json(frame)
                now = time.monotonic()
                if now - last_persisted >= 2.0:
                    await upsert_browser_task_live_frame(
                        tenant_id=tenant_id,
                        task_id=task_id,
                        frame_base64=str(frame.get("frame") or ""),
                        media_type="image/jpeg",
                        width=live.width,
                        height=live.height,
                        current_url=str(state_now.get("url") or ""),
                        page_title=str(state_now.get("title") or ""),
                        current_step="server CDP live stream",
                        metadata={"source": "server_cdp_screencast", "interactive": True},
                    )
                    last_persisted = now

        async def receive_controls() -> None:
            while True:
                message = await websocket.receive_json()
                message_type = str(message.get("type") or "")
                if message_type == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                if message_type != "control":
                    await websocket.send_json({"type": "error", "error": "unsupported_message_type"})
                    continue
                try:
                    result = await live.apply_control(message)
                except BrowserLiveControlError as exc:
                    await websocket.send_json({"type": "control_error", "error": str(exc)})
                    continue
                recipe_step = result.get("recipe_step") if isinstance(result, dict) else None
                await record_browser_task_step(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    step="action",
                    narration=f"대표님이 브라우저에서 {result.get('action') or 'control'} 조작을 수행했습니다.",
                    guide="성공한 조작은 학습 모드에서 승인 대기 레시피 단계로 기록됩니다.",
                    route="server_cdp_input",
                    extra={
                        "action": result.get("action") or "",
                        "selector": (recipe_step or {}).get("selector") if isinstance(recipe_step, dict) else "",
                        "secret": bool(result.get("secret")),
                    },
                )
                await websocket.send_json({"type": "control_ack", "result": result})

        sender = asyncio.create_task(send_frames())
        receiver = asyncio.create_task(receive_controls())
        done, pending = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for pending_task in pending:
            pending_task.cancel()
        for finished in done:
            finished.result()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - Playwright/CDP exposes runtime-specific errors.
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "error": str(exc)[:300]})
    finally:
        await live.close()


@router.post("/{task_id}/live-frame")
async def api_update_browser_task_live_frame(
    task_id: str,
    body: BrowserLiveFrameIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    if not body.frame_base64 and not body.frame_url:
        raise HTTPException(status_code=400, detail="frame_base64_or_frame_url_required")
    task = await get_browser_task(tenant_id=_tenant_id(context), task_id=task_id)
    if not task:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    session_id = str(task.get("session_id") or "")
    if not session_id:
        raise HTTPException(status_code=409, detail="MISSING_SESSION_ID")
    # Persist structural provenance with the frame metadata.  Any later LLM,
    # Browser, or PC consumer can therefore reject it at its own execution
    # boundary; a page string cannot erase this server-added marker.
    tainted_metadata = {
        **body.metadata,
        "__aads_taint__": "UNTRUSTED_PAGE_DATA",
        "observation_source": "screenshot_ocr",
    }
    observation_payload = {
        "frame_sha256": payload_hash({"frame": body.frame_base64 or body.frame_url}),
        "current_url": body.current_url,
        "page_title": body.page_title,
        "current_step": body.current_step,
        "metadata": tainted_metadata,
    }
    ChannelRouter().route_observation(
        ObservationEnvelope(
            source="screenshot_ocr",
            tenant_id=_tenant_id(context),
            session_id=session_id,
            correlation_id=body.correlation_id or f"browser-frame:{task_id}",
            trust_level="untrusted",
            payload=observation_payload,
            payload_hash=payload_hash(observation_payload),
        )
    )
    frame = await upsert_browser_task_live_frame(
        tenant_id=_tenant_id(context),
        task_id=task_id,
        frame_base64=body.frame_base64,
        frame_url=body.frame_url,
        media_type=body.media_type,
        width=body.width,
        height=body.height,
        current_url=body.current_url,
        page_title=body.page_title,
        current_step=body.current_step,
        cursor=body.cursor,
        metadata=tainted_metadata,
    )
    if not frame:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    return {"status": "updated", "frame": frame}


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
    return await _display({"status": "updated", "task": task}, context)


@router.post("/{task_id}/retry")
async def api_retry_browser_task(
    task_id: str,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    current = await get_browser_task(tenant_id=_tenant_id(context), task_id=task_id)
    if not current:
        raise HTTPException(status_code=404, detail="browser_task_not_found")
    if current.get("status") in {"running", "queued"}:
        return {"status": "already_active", "task": current}
    if current.get("status") in {"approval_required", "auth_required"}:
        raise HTTPException(status_code=409, detail="approval_or_authentication_required")
    task = await update_browser_task_status(
        tenant_id=_tenant_id(context), task_id=task_id, status="queued",
        current_step="재시도 대기", result={"retry_requested": True}, error="",
    )
    return {"status": "queued", "task": task}


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
