"""단방향 알림 인입구 — 외부 cron 이 담당 세션 인박스로 알림을 넣는다.

2026-09-29. 텔레그램 대신 담당 세션이 알림을 받아 조치하게 한다. 세션에
들어가는 길은 MCP `ask_session`(왕복) 하나뿐이었고 HTTP 인입구가 없었다.
알림은 답이 필요 없으므로 왕복 경로와 분리한다 — 로직은
`app/services/session_relay.notify`.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role

router = APIRouter()
require_tenant_member = require_tenant_role(TenantRole.MEMBER)


class SessionNotifyRequest(BaseModel):
    target: str = Field(..., min_length=1, max_length=200)
    title: str = Field(..., min_length=1, max_length=300)
    body: str = Field(..., min_length=1)
    severity: Literal["info", "warn", "critical"] = "info"
    source: str = Field(..., min_length=1, max_length=200)
    dedup_key: Optional[str] = Field(default=None, max_length=200)
    workspace_id: Optional[UUID] = None


class SessionNotifyResponse(BaseModel):
    delivered: bool
    target_session_id: Optional[str] = None
    relay_id: Optional[str] = None
    reason: Optional[str] = None


@router.post("/session-notify", status_code=202, response_model=SessionNotifyResponse)
async def post_session_notify(
    payload: SessionNotifyRequest,
    context: dict[str, Any] = Depends(require_tenant_member),
) -> dict[str, Any]:
    from app.services.session_relay import notify

    tenant_id = str((context.get("tenant") or {}).get("id") or "").strip() or None
    return await notify(
        target=payload.target,
        title=payload.title,
        body=payload.body,
        severity=payload.severity,
        source=payload.source,
        dedup_key=(payload.dedup_key or "").strip() or None,
        workspace_id=str(payload.workspace_id) if payload.workspace_id else None,
        tenant_id=tenant_id,
    )
