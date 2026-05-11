"""API for Design Modification Studio foundation."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.core.db_pool import get_pool
from app.services.design_context_builder import DesignContextRequestNotFound
from app.services.design_context_builder import build_context_pack
from app.services.design_qa_scorer import DesignModificationRequestNotFoundError
from app.services.design_qa_scorer import score_modification

router = APIRouter(prefix="/admin/design", tags=["design-modifications"])

_REQUEST_STATUSES = {
    "draft",
    "ready",
    "running",
    "review",
    "approved",
    "rejected",
}
_REQUEST_TYPES = {
    "spacing",
    "spacing_density",
    "visual_hierarchy",
    "color",
    "color_brand",
    "typography",
    "component",
    "component_consistency",
    "responsive",
    "interaction",
    "content_clarity",
    "workflow_layout",
    "flow",
    "other",
}


def _require_user_id(current_user: dict[str, Any]) -> str:
    user_id = str(current_user.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid user context")
    return user_id


def _normalize_project_key(project_key: str) -> str:
    normalized = (project_key or "").strip().upper()
    if not normalized:
        raise HTTPException(status_code=400, detail="project_key is required")
    return normalized


def _validate_status(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = (status or "").strip().lower()
    if not normalized:
        return None
    if normalized not in _REQUEST_STATUSES:
        raise HTTPException(status_code=400, detail=f"unsupported status '{status}'")
    return normalized


def _validate_request_type(request_type: str | None) -> str:
    normalized = (request_type or "other").strip().lower()
    if not normalized:
        return "other"
    if normalized not in _REQUEST_TYPES:
        raise HTTPException(status_code=400, detail=f"unsupported request_type '{request_type}'")
    return normalized


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_json_value(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return fallback
        return parsed if isinstance(parsed, (dict, list)) else fallback
    return fallback if value is None else value


def _json_size(value: Any) -> int:
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, list):
        return len(value)
    return 0


def _excerpt(value: str, limit: int = 180) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


class DesignScreenSummary(BaseModel):
    id: str
    project_key: str
    route: str
    name: str
    purpose: str
    primary_actions: list[Any] = Field(default_factory=list)
    component_paths: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class DesignScreenListResponse(BaseModel):
    project_key: str
    screens: list[DesignScreenSummary] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class DesignScreenRef(BaseModel):
    id: str
    route: str
    name: str
    purpose: str
    primary_actions: list[Any] = Field(default_factory=list)
    component_paths: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DesignVisualSnapshotSummary(BaseModel):
    id: str
    request_id: str
    phase: str
    viewport: str
    image_url: str
    dom_summary: Any = Field(default_factory=dict)
    captured_at: datetime


class DesignDecisionSummary(BaseModel):
    id: str
    project_key: str
    screen_id: str | None = None
    subject: str
    decision: str
    rationale: str | None = None
    applies_to: str
    confidence: float
    supersedes_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class DesignModificationRequestSummary(BaseModel):
    id: str
    project_key: str
    screen_id: str | None = None
    screen_route: str | None = None
    screen_name: str | None = None
    request_type: str
    status: str
    prompt_excerpt: str
    acceptance_criteria_count: int
    context_pack_count: int
    latest_context_pack_created_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DesignModificationRequestListResponse(BaseModel):
    project_key: str
    requests: list[DesignModificationRequestSummary] = Field(default_factory=list)
    total: int
    limit: int
    offset: int
    status: str | None = None
    screen_id: str | None = None


class DesignModificationRequestDetail(BaseModel):
    id: str
    project_key: str
    screen_id: str | None = None
    screen: DesignScreenRef | None = None
    user_prompt: str
    normalized_card: dict[str, Any] = Field(default_factory=dict)
    request_type: str
    allowed_scope: Any = Field(default_factory=dict)
    forbidden_scope: Any = Field(default_factory=dict)
    acceptance_criteria: list[Any] = Field(default_factory=list)
    status: str
    context_pack_count: int
    created_at: datetime
    updated_at: datetime


class DesignModificationRequestDetailResponse(BaseModel):
    request: DesignModificationRequestDetail
    snapshots: list[DesignVisualSnapshotSummary] = Field(default_factory=list)
    decisions: list[DesignDecisionSummary] = Field(default_factory=list)


class DesignModificationRequestCreate(BaseModel):
    project_key: str
    screen_id: UUID | None = None
    user_prompt: str = Field(..., min_length=1)
    normalized_card: dict[str, Any] = Field(default_factory=dict)
    request_type: str = "other"
    allowed_scope: Any = Field(default_factory=dict)
    forbidden_scope: Any = Field(default_factory=dict)
    acceptance_criteria: list[Any] = Field(default_factory=list)
    status: str = "draft"


class DesignModificationRequestCreateResponse(BaseModel):
    request: DesignModificationRequestDetail


class DesignContextPackSummary(BaseModel):
    id: str
    request_id: str
    source_count: int
    missing_context_count: int
    prompt_chars: int
    sources: Any = Field(default_factory=list)
    missing_context: list[Any] = Field(default_factory=list)
    created_at: datetime


class DesignContextPackListResponse(BaseModel):
    request_id: str
    context_packs: list[DesignContextPackSummary] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class DesignContextPackPreview(BaseModel):
    id: str
    request_id: str
    project_key: str
    screen_id: str | None = None
    screen_route: str | None = None
    screen_name: str | None = None
    request_type: str
    status: str
    user_prompt_excerpt: str
    source_count: int
    missing_context_count: int
    context: dict[str, Any] = Field(default_factory=dict)
    sources: Any = Field(default_factory=list)
    missing_context: list[Any] = Field(default_factory=list)
    prompt_chars: int
    created_at: datetime


class DesignContextPackPreviewResponse(BaseModel):
    context_pack: DesignContextPackPreview


def _serialize_screen(row: dict[str, Any]) -> DesignScreenSummary:
    return DesignScreenSummary(
        id=str(row["id"]),
        project_key=str(row["project_key"]),
        route=str(row["route"]),
        name=str(row["name"]),
        purpose=str(row.get("purpose") or ""),
        primary_actions=_as_list(row.get("primary_actions")),
        component_paths=_as_list(row.get("component_paths")),
        metadata=_as_dict(row.get("metadata")),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _serialize_screen_ref(row: dict[str, Any]) -> DesignScreenRef | None:
    if not row.get("screen_id"):
        return None
    return DesignScreenRef(
        id=str(row["screen_id"]),
        route=str(row.get("screen_route") or ""),
        name=str(row.get("screen_name") or ""),
        purpose=str(row.get("screen_purpose") or ""),
        primary_actions=_as_list(row.get("screen_primary_actions")),
        component_paths=_as_list(row.get("screen_component_paths")),
        metadata=_as_dict(row.get("screen_metadata")),
    )


def _serialize_request_summary(row: dict[str, Any]) -> DesignModificationRequestSummary:
    return DesignModificationRequestSummary(
        id=str(row["id"]),
        project_key=str(row["project_key"]),
        screen_id=str(row["screen_id"]) if row.get("screen_id") else None,
        screen_route=row.get("screen_route"),
        screen_name=row.get("screen_name"),
        request_type=str(row.get("request_type") or "other"),
        status=str(row.get("status") or "draft"),
        prompt_excerpt=_excerpt(str(row.get("user_prompt") or "")),
        acceptance_criteria_count=len(_as_list(row.get("acceptance_criteria"))),
        context_pack_count=int(row.get("context_pack_count") or 0),
        latest_context_pack_created_at=row.get("latest_context_pack_created_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _serialize_snapshot(row: dict[str, Any]) -> DesignVisualSnapshotSummary:
    return DesignVisualSnapshotSummary(
        id=str(row["id"]),
        request_id=str(row["request_id"]),
        phase=str(row.get("phase") or "before"),
        viewport=str(row.get("viewport") or ""),
        image_url=str(row.get("image_url") or ""),
        dom_summary=row.get("dom_summary") or {},
        captured_at=row["captured_at"],
    )


def _serialize_decision(row: dict[str, Any]) -> DesignDecisionSummary:
    return DesignDecisionSummary(
        id=str(row["id"]),
        project_key=str(row["project_key"]),
        screen_id=str(row["screen_id"]) if row.get("screen_id") else None,
        subject=str(row.get("subject") or ""),
        decision=str(row.get("decision") or ""),
        rationale=row.get("rationale"),
        applies_to=str(row.get("applies_to") or "project"),
        confidence=float(row.get("confidence") or 0.0),
        supersedes_id=str(row["supersedes_id"]) if row.get("supersedes_id") else None,
        metadata=_as_dict(row.get("metadata")),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _serialize_context_pack_summary(row: dict[str, Any]) -> DesignContextPackSummary:
    sources = row.get("sources")
    missing_context = _as_list(row.get("missing_context"))
    return DesignContextPackSummary(
        id=str(row["id"]),
        request_id=str(row["request_id"]),
        source_count=_json_size(sources),
        missing_context_count=len(missing_context),
        prompt_chars=int(row.get("prompt_chars") or 0),
        sources=sources if sources is not None else [],
        missing_context=missing_context,
        created_at=row["created_at"],
    )


def _schema_unavailable(_exc: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail="design modification schema is not initialized")


@router.post(
    "/modification-requests",
    response_model=DesignModificationRequestCreateResponse,
    status_code=201,
)
async def create_design_modification_request(
    payload: DesignModificationRequestCreate,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> DesignModificationRequestCreateResponse:
    _require_user_id(current_user)
    normalized_project_key = _normalize_project_key(payload.project_key)
    normalized_request_type = _validate_request_type(payload.request_type)
    normalized_status = _validate_status(payload.status) or "draft"
    user_prompt = payload.user_prompt.strip()
    if not user_prompt:
        raise HTTPException(status_code=400, detail="user_prompt is required")

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH inserted AS (
                    INSERT INTO design_modification_requests (
                        project_key, screen_id, user_prompt, normalized_card,
                        request_type, allowed_scope, forbidden_scope,
                        acceptance_criteria, status
                    )
                    VALUES (
                        $1, $2::uuid, $3, $4::jsonb,
                        $5, $6::jsonb, $7::jsonb,
                        $8::jsonb, $9
                    )
                    RETURNING id, project_key, screen_id, user_prompt, normalized_card,
                              request_type, allowed_scope, forbidden_scope,
                              acceptance_criteria, status, created_at, updated_at
                )
                SELECT i.id, i.project_key, i.screen_id, i.user_prompt, i.normalized_card,
                       i.request_type, i.allowed_scope, i.forbidden_scope,
                       i.acceptance_criteria, i.status, i.created_at, i.updated_at,
                       s.route AS screen_route, s.name AS screen_name, s.purpose AS screen_purpose,
                       s.primary_actions AS screen_primary_actions,
                       s.component_paths AS screen_component_paths,
                       s.metadata AS screen_metadata,
                       0::int AS context_pack_count
                FROM inserted i
                LEFT JOIN design_screens s
                    ON s.id = i.screen_id
                """,
                normalized_project_key,
                payload.screen_id,
                user_prompt,
                json.dumps(payload.normalized_card, ensure_ascii=False),
                normalized_request_type,
                json.dumps(payload.allowed_scope, ensure_ascii=False),
                json.dumps(payload.forbidden_scope, ensure_ascii=False),
                json.dumps(payload.acceptance_criteria, ensure_ascii=False),
                normalized_status,
            )
    except asyncpg.UndefinedTableError as exc:
        raise _schema_unavailable(exc)
    except asyncpg.ForeignKeyViolationError as exc:
        raise HTTPException(status_code=400, detail="project_key or screen_id does not exist") from exc
    except asyncpg.CheckViolationError as exc:
        raise HTTPException(status_code=400, detail="invalid design modification request") from exc

    detail_row = dict(row)
    request = DesignModificationRequestDetail(
        id=str(detail_row["id"]),
        project_key=str(detail_row["project_key"]),
        screen_id=str(detail_row["screen_id"]) if detail_row.get("screen_id") else None,
        screen=_serialize_screen_ref(detail_row),
        user_prompt=str(detail_row.get("user_prompt") or ""),
        normalized_card=_as_dict(detail_row.get("normalized_card")),
        request_type=str(detail_row.get("request_type") or "other"),
        allowed_scope=_as_json_value(detail_row.get("allowed_scope"), {}),
        forbidden_scope=_as_json_value(detail_row.get("forbidden_scope"), {}),
        acceptance_criteria=_as_list(detail_row.get("acceptance_criteria")),
        status=str(detail_row.get("status") or "draft"),
        context_pack_count=int(detail_row.get("context_pack_count") or 0),
        created_at=detail_row["created_at"],
        updated_at=detail_row["updated_at"],
    )
    return DesignModificationRequestCreateResponse(request=request)


@router.get("/projects/{project_key}/screens", response_model=DesignScreenListResponse)
async def list_design_screens(
    project_key: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> DesignScreenListResponse:
    _require_user_id(current_user)
    normalized_project_key = _normalize_project_key(project_key)
    pool = get_pool()

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.id, s.project_key, s.route, s.name, s.purpose, s.primary_actions,
                       s.component_paths, s.metadata, s.created_at, s.updated_at
                FROM design_screens s
                WHERE s.project_key = $1
                ORDER BY s.route, s.created_at DESC
                LIMIT $2 OFFSET $3
                """,
                normalized_project_key,
                limit,
                offset,
            )
            total = await conn.fetchval(
                """
                SELECT COUNT(*)::int
                FROM design_screens
                WHERE project_key = $1
                """,
                normalized_project_key,
            )
    except asyncpg.UndefinedTableError:
        return DesignScreenListResponse(
            project_key=normalized_project_key,
            screens=[],
            total=0,
            limit=limit,
            offset=offset,
        )

    return DesignScreenListResponse(
        project_key=normalized_project_key,
        screens=[_serialize_screen(dict(row)) for row in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/projects/{project_key}/modification-requests",
    response_model=DesignModificationRequestListResponse,
)
async def list_design_modification_requests(
    project_key: str,
    status: str | None = Query(None, description="draft|ready|running|review|approved|rejected"),
    screen_id: UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> DesignModificationRequestListResponse:
    _require_user_id(current_user)
    normalized_project_key = _normalize_project_key(project_key)
    normalized_status = _validate_status(status)
    pool = get_pool()

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT r.id, r.project_key, r.screen_id, r.user_prompt, r.acceptance_criteria,
                       r.request_type, r.status, r.created_at, r.updated_at,
                       s.route AS screen_route, s.name AS screen_name,
                       COALESCE(cp.context_pack_count, 0) AS context_pack_count,
                       cp.latest_context_pack_created_at
                FROM design_modification_requests r
                LEFT JOIN design_screens s
                    ON s.id = r.screen_id
                LEFT JOIN LATERAL (
                    SELECT COUNT(*)::int AS context_pack_count,
                           MAX(created_at) AS latest_context_pack_created_at
                    FROM design_context_packs
                    WHERE request_id = r.id
                ) cp ON TRUE
                WHERE r.project_key = $1
                  AND ($2::text IS NULL OR r.status = $2)
                  AND ($3::uuid IS NULL OR r.screen_id = $3::uuid)
                ORDER BY r.created_at DESC
                LIMIT $4 OFFSET $5
                """,
                normalized_project_key,
                normalized_status,
                screen_id,
                limit,
                offset,
            )
            total = await conn.fetchval(
                """
                SELECT COUNT(*)::int
                FROM design_modification_requests
                WHERE project_key = $1
                  AND ($2::text IS NULL OR status = $2)
                  AND ($3::uuid IS NULL OR screen_id = $3::uuid)
                """,
                normalized_project_key,
                normalized_status,
                screen_id,
            )
    except asyncpg.UndefinedTableError:
        return DesignModificationRequestListResponse(
            project_key=normalized_project_key,
            requests=[],
            total=0,
            limit=limit,
            offset=offset,
            status=normalized_status,
            screen_id=str(screen_id) if screen_id else None,
        )

    return DesignModificationRequestListResponse(
        project_key=normalized_project_key,
        requests=[_serialize_request_summary(dict(row)) for row in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
        status=normalized_status,
        screen_id=str(screen_id) if screen_id else None,
    )


@router.get(
    "/modification-requests/{request_id}",
    response_model=DesignModificationRequestDetailResponse,
)
async def get_design_modification_request(
    request_id: UUID,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> DesignModificationRequestDetailResponse:
    _require_user_id(current_user)
    pool = get_pool()

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT r.id, r.project_key, r.screen_id, r.user_prompt, r.normalized_card,
                       r.request_type, r.allowed_scope, r.forbidden_scope, r.acceptance_criteria,
                       r.status, r.created_at, r.updated_at,
                       s.route AS screen_route, s.name AS screen_name, s.purpose AS screen_purpose,
                       s.primary_actions AS screen_primary_actions,
                       s.component_paths AS screen_component_paths,
                       s.metadata AS screen_metadata,
                       (
                           SELECT COUNT(*)::int
                           FROM design_context_packs
                           WHERE request_id = r.id
                       ) AS context_pack_count
                FROM design_modification_requests r
                LEFT JOIN design_screens s
                    ON s.id = r.screen_id
                WHERE r.id = $1::uuid
                """,
                request_id,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="design modification request not found")

            snapshot_rows = await conn.fetch(
                """
                SELECT id, request_id, phase, viewport, image_url, dom_summary, captured_at
                FROM design_visual_snapshots
                WHERE request_id = $1::uuid
                ORDER BY captured_at DESC
                LIMIT 24
                """,
                request_id,
            )
            decision_rows = await conn.fetch(
                """
                SELECT id, project_key, screen_id, subject, decision, rationale,
                       applies_to, confidence, supersedes_id, metadata, created_at, updated_at
                FROM design_decisions
                WHERE project_key = $1
                  AND (screen_id = $2::uuid OR screen_id IS NULL)
                ORDER BY
                    CASE applies_to
                        WHEN 'screen' THEN 0
                        WHEN 'component' THEN 1
                        WHEN 'project' THEN 2
                        ELSE 3
                    END,
                    created_at DESC
                LIMIT 24
                """,
                row["project_key"],
                row["screen_id"],
            )
    except asyncpg.UndefinedTableError as exc:
        raise _schema_unavailable(exc)

    detail_row = dict(row)
    request = DesignModificationRequestDetail(
        id=str(detail_row["id"]),
        project_key=str(detail_row["project_key"]),
        screen_id=str(detail_row["screen_id"]) if detail_row.get("screen_id") else None,
        screen=_serialize_screen_ref(detail_row),
        user_prompt=str(detail_row.get("user_prompt") or ""),
        normalized_card=_as_dict(detail_row.get("normalized_card")),
        request_type=str(detail_row.get("request_type") or "other"),
        allowed_scope=_as_json_value(detail_row.get("allowed_scope"), {}),
        forbidden_scope=_as_json_value(detail_row.get("forbidden_scope"), {}),
        acceptance_criteria=_as_list(detail_row.get("acceptance_criteria")),
        status=str(detail_row.get("status") or "draft"),
        context_pack_count=int(detail_row.get("context_pack_count") or 0),
        created_at=detail_row["created_at"],
        updated_at=detail_row["updated_at"],
    )

    return DesignModificationRequestDetailResponse(
        request=request,
        snapshots=[_serialize_snapshot(dict(item)) for item in snapshot_rows],
        decisions=[_serialize_decision(dict(item)) for item in decision_rows],
    )


@router.post("/modification-requests/{request_id}/build-context")
async def build_design_modification_context_pack(
    request_id: UUID,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    _require_user_id(current_user)
    try:
        return await build_context_pack(request_id)
    except DesignContextRequestNotFound as exc:
        raise HTTPException(status_code=404, detail="design modification request not found") from exc
    except asyncpg.UndefinedTableError as exc:
        raise _schema_unavailable(exc)


@router.post("/modification-requests/{request_id}/score")
async def score_design_modification_request(
    request_id: UUID,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    _require_user_id(current_user)
    try:
        return await score_modification(request_id)
    except DesignModificationRequestNotFoundError as exc:
        raise HTTPException(status_code=404, detail="design modification request not found") from exc
    except asyncpg.UndefinedTableError as exc:
        raise _schema_unavailable(exc)


@router.get(
    "/modification-requests/{request_id}/context-packs",
    response_model=DesignContextPackListResponse,
)
async def list_design_context_packs(
    request_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> DesignContextPackListResponse:
    _require_user_id(current_user)
    pool = get_pool()

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT cp.id, cp.request_id, cp.sources, cp.missing_context,
                       cp.prompt_chars, cp.created_at
                FROM design_context_packs cp
                WHERE cp.request_id = $1::uuid
                ORDER BY cp.created_at DESC
                LIMIT $2 OFFSET $3
                """,
                request_id,
                limit,
                offset,
            )
            total = await conn.fetchval(
                """
                SELECT COUNT(*)::int
                FROM design_context_packs
                WHERE request_id = $1::uuid
                """,
                request_id,
            )
    except asyncpg.UndefinedTableError:
        return DesignContextPackListResponse(
            request_id=str(request_id),
            context_packs=[],
            total=0,
            limit=limit,
            offset=offset,
        )

    return DesignContextPackListResponse(
        request_id=str(request_id),
        context_packs=[_serialize_context_pack_summary(dict(row)) for row in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/context-packs/{context_pack_id}/preview",
    response_model=DesignContextPackPreviewResponse,
)
async def preview_design_context_pack(
    context_pack_id: UUID,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> DesignContextPackPreviewResponse:
    _require_user_id(current_user)
    pool = get_pool()

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT cp.id, cp.request_id, cp.context, cp.sources, cp.missing_context,
                       cp.prompt_chars, cp.created_at,
                       r.project_key, r.screen_id, r.request_type, r.status, r.user_prompt,
                       s.route AS screen_route, s.name AS screen_name
                FROM design_context_packs cp
                JOIN design_modification_requests r
                    ON r.id = cp.request_id
                LEFT JOIN design_screens s
                    ON s.id = r.screen_id
                WHERE cp.id = $1::uuid
                """,
                context_pack_id,
            )
    except asyncpg.UndefinedTableError as exc:
        raise _schema_unavailable(exc)

    if row is None:
        raise HTTPException(status_code=404, detail="design context pack not found")

    payload = dict(row)
    sources = payload.get("sources")
    missing_context = _as_list(payload.get("missing_context"))
    preview = DesignContextPackPreview(
        id=str(payload["id"]),
        request_id=str(payload["request_id"]),
        project_key=str(payload["project_key"]),
        screen_id=str(payload["screen_id"]) if payload.get("screen_id") else None,
        screen_route=payload.get("screen_route"),
        screen_name=payload.get("screen_name"),
        request_type=str(payload.get("request_type") or "other"),
        status=str(payload.get("status") or "draft"),
        user_prompt_excerpt=_excerpt(str(payload.get("user_prompt") or "")),
        source_count=_json_size(sources),
        missing_context_count=len(missing_context),
        context=_as_dict(payload.get("context")),
        sources=sources if sources is not None else [],
        missing_context=missing_context,
        prompt_chars=int(payload.get("prompt_chars") or 0),
        created_at=payload["created_at"],
    )
    return DesignContextPackPreviewResponse(context_pack=preview)
