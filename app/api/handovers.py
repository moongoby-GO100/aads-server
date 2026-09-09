"""Global handover ledger REST API."""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status as http_status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.services.handover_store import (
    HandoverConflictError,
    HandoverNotFoundError,
    get_handover_entry,
    list_handover_entries,
    list_handover_events,
    parse_markdown_sections,
    render_handover_markdown,
    upsert_handover_entry,
)

router = APIRouter(prefix="/handovers", tags=["handovers"])
TenantContext = dict[str, object]
require_tenant_viewer = require_tenant_role(TenantRole.VIEWER)
require_tenant_member = require_tenant_role(TenantRole.MEMBER)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])  # type: ignore[index]


def _actor(context: TenantContext) -> str:
    user = context.get("user") or {}
    return str(user.get("email") or user.get("id") or "handover_api") if isinstance(user, dict) else "handover_api"


class HandoverUpsertRequest(BaseModel):
    project_key: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=1_000_000)
    entry_key: str | None = Field(default=None, max_length=200)
    entry_type: Literal["status", "decision", "task", "risk", "verification", "note"] = "note"
    summary: str | None = Field(default=None, max_length=2_000)
    status: Literal["active", "resolved", "superseded", "archived"] = "active"
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    source_task_id: str | None = None
    source_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    change_summary: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class HandoverImportRequest(BaseModel):
    project_key: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=5_000_000)
    source_path: str = Field(default="HANDOVER.md", min_length=1)
    default_type: Literal["status", "decision", "task", "risk", "verification", "note"] = "note"
    default_priority: Literal["P0", "P1", "P2", "P3"] = "P2"


@router.post("", status_code=http_status.HTTP_201_CREATED, operation_id="write_handover")
async def write_handover(
    body: HandoverUpsertRequest,
    response: Response,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    try:
        entry, changed = await upsert_handover_entry(
            tenant_id=_tenant_id(context),
            project_key=body.project_key,
            title=body.title,
            body=body.body,
            entry_key=body.entry_key,
            entry_type=body.entry_type,
            summary=body.summary,
            status=body.status,
            priority=body.priority,
            source_kind="api",
            source_task_id=body.source_task_id,
            source_path=body.source_path,
            metadata=body.metadata,
            changed_by=_actor(context),
            change_summary=body.change_summary,
            expected_revision=body.expected_revision,
        )
    except HandoverConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not changed:
        response.status_code = http_status.HTTP_200_OK
    return {"entry": entry, "changed": changed}


@router.get("", operation_id="search_handovers")
async def search_handovers(
    project_key: str | None = None,
    status: str | None = None,
    entry_type: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    context: TenantContext = Depends(require_tenant_viewer),
) -> dict[str, Any]:
    try:
        items, total = await list_handover_entries(
            tenant_id=_tenant_id(context), project_key=project_key, status=status,
            entry_type=entry_type, query=q, limit=limit, offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/export", operation_id="export_handover", response_class=PlainTextResponse)
async def export_handover(
    project_key: str,
    include_archived: bool = False,
    context: TenantContext = Depends(require_tenant_viewer),
) -> PlainTextResponse:
    items, _ = await list_handover_entries(
        tenant_id=_tenant_id(context), project_key=project_key,
        status=None if include_archived else "active", limit=500,
    )
    markdown = render_handover_markdown(items, project_key=project_key)
    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{project_key.upper()}-HANDOVER.md"'},
    )


@router.post("/import", operation_id="import_handover")
async def import_handover(
    body: HandoverImportRequest,
    context: TenantContext = Depends(require_tenant_member),
) -> dict[str, Any]:
    sections = parse_markdown_sections(body.content, source_path=body.source_path)
    changed = 0
    ids: list[str] = []
    for section in sections:
        entry, did_change = await upsert_handover_entry(
            tenant_id=_tenant_id(context), project_key=body.project_key,
            entry_key=section["entry_key"], title=section["title"], body=section["body"],
            entry_type=body.default_type, priority=body.default_priority,
            source_kind="markdown_import", source_path=body.source_path,
            metadata={"legacy_import": True}, changed_by=_actor(context),
            change_summary=f"Imported from {body.source_path}", event_type="imported",
        )
        changed += int(did_change)
        ids.append(entry["id"])
    return {"project_key": body.project_key.upper(), "sections": len(sections), "changed": changed, "entry_ids": ids}


@router.get("/{entry_id}", operation_id="get_handover")
async def get_handover(
    entry_id: UUID,
    context: TenantContext = Depends(require_tenant_viewer),
) -> dict[str, Any]:
    try:
        return await get_handover_entry(tenant_id=_tenant_id(context), entry_id=str(entry_id))
    except HandoverNotFoundError as exc:
        raise HTTPException(404, "Handover entry not found") from exc


@router.get("/{entry_id}/events", operation_id="get_handover_events")
async def get_handover_events(
    entry_id: UUID,
    context: TenantContext = Depends(require_tenant_viewer),
) -> dict[str, Any]:
    try:
        await get_handover_entry(tenant_id=_tenant_id(context), entry_id=str(entry_id))
    except HandoverNotFoundError as exc:
        raise HTTPException(404, "Handover entry not found") from exc
    items = await list_handover_events(tenant_id=_tenant_id(context), entry_id=str(entry_id))
    return {"items": items, "total": len(items)}
