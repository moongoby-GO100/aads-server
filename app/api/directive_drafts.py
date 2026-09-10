"""Review-first directive draft API for the chat composer and artifact panel."""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.services import directive_draft_service as drafts


router = APIRouter(prefix="/chat", tags=["directive-drafts"])
TenantContext = dict[str, Any]
require_tenant_viewer = require_tenant_role(TenantRole.VIEWER)
require_tenant_member = require_tenant_role(TenantRole.MEMBER)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _user_id(context: TenantContext) -> str | None:
    user = context.get("user") or {}
    value = user.get("user_id") or user.get("id")
    return str(value) if value else None


class DraftCreateRequest(BaseModel):
    context_window: int = Field(default=8, ge=2, le=16)
    message_ids: list[UUID] | None = Field(default=None, max_length=16)


class DraftUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=50_000)
    expected_revision: int | None = Field(default=None, ge=1)


class DraftEventRequest(BaseModel):
    action: Literal["inserted", "approved", "rejected", "sent", "archived"]
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post(
    "/sessions/{session_id}/directive-drafts",
    status_code=status.HTTP_201_CREATED,
)
async def create_directive_draft(
    session_id: UUID,
    body: DraftCreateRequest,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    try:
        return await drafts.create_draft(
            tenant_id=_tenant_id(context),
            user_id=_user_id(context),
            session_id=str(session_id),
            context_window=body.context_window,
            message_ids=[str(value) for value in body.message_ids] if body.message_ids else None,
        )
    except drafts.DraftNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/sessions/{session_id}/directive-drafts")
async def list_directive_drafts(
    session_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    context: TenantContext = Depends(require_tenant_viewer),
) -> dict[str, Any]:
    items = await drafts.list_drafts(
        tenant_id=_tenant_id(context), session_id=str(session_id), limit=limit
    )
    return {"items": items}


@router.patch("/directive-drafts/{draft_id}")
async def update_directive_draft(
    draft_id: UUID,
    body: DraftUpdateRequest,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    if body.title is None and body.content is None:
        raise HTTPException(422, "title or content is required")
    try:
        return await drafts.update_draft(
            tenant_id=_tenant_id(context),
            user_id=_user_id(context),
            draft_id=str(draft_id),
            title=body.title,
            content=body.content,
            expected_revision=body.expected_revision,
        )
    except drafts.DraftNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except drafts.DraftConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/directive-drafts/{draft_id}/events")
async def record_directive_draft_event(
    draft_id: UUID,
    body: DraftEventRequest,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    try:
        return await drafts.record_event(
            tenant_id=_tenant_id(context),
            user_id=_user_id(context),
            draft_id=str(draft_id),
            action=body.action,
            metadata=body.metadata,
        )
    except drafts.DraftNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
