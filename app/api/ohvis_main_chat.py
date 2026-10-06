"""OHVIS main chat API, mounted in app/main.py under /api/v1.

Every endpoint takes the tenant from the authenticated context. Writes that change routing, grants or the pause
switch need an elevated role; reading and acknowledging notices needs only project access. Everything but the
pause switch answers 503 until OHVIS_MAIN_CHAT_ENABLED is set, and every endpoint answers 503 while the
migration has not been applied.
"""
from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.routing import APIRoute

from app.api.canonical_documents import VIEW, WRITE, _authorize, _project, _scope
from app.core.db_pool import get_pool
from app.models.ohvis_main_chat import ControlRequest, GrantCreate, GrantRevoke, ReportEvent, RouteUpsert
from app.services import ohvis_main_chat_service as svc


class _MigrationAwareRoute(APIRoute):
    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await handler(request)
            except asyncpg.UndefinedTableError as exc:
                raise HTTPException(503, {"error": "main_chat_not_migrated"}) from exc

        return guarded


router = APIRouter(prefix="/projects/{project_key}/main-chat", tags=["ohvis-main-chat"], route_class=_MigrationAwareRoute)


def _http(exc: svc.MainChatError) -> HTTPException:
    return HTTPException(exc.status, {"error": exc.code, **exc.extra})


def _require_enabled() -> None:
    if not svc.load_flags().enabled:
        raise HTTPException(503, {"error": "main_chat_disabled"})


def _require_elevated(context: dict) -> None:
    if not _scope(context)[2]:
        raise HTTPException(403, {"error": "admin_role_required"})


@router.get("")
async def overview(project_key: str, limit: int = Query(50, ge=1, le=200), context: dict = VIEW) -> dict[str, Any]:
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        return await svc.list_cards(conn, tenant, project, limit)


@router.put("/routes")
async def put_route(project_key: str, body: RouteUpsert, context: dict = WRITE) -> dict[str, Any]:
    _require_enabled()
    _require_elevated(context)
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, actor = await _authorize(conn, context, project, "approve")
        try:
            return await svc.upsert_route(conn, tenant, actor, project, body)
        except svc.MainChatError as exc:
            raise _http(exc) from exc


@router.post("/reports", status_code=202)
async def post_report(project_key: str, body: ReportEvent, context: dict = WRITE) -> dict[str, Any]:
    _require_enabled()
    project = _project(project_key)
    if body.project_key != project:
        raise HTTPException(422, {"error": "project_key_mismatch"})
    if body.source_kind == "runner":
        _require_elevated(context)
    async with get_pool().acquire() as conn:
        tenant, actor = await _authorize(conn, context, project, "write")
        try:
            return await svc.ingest(conn, tenant, actor, body)
        except svc.MainChatError as exc:
            raise _http(exc) from exc


@router.post("/evidence/requeue")
async def requeue_evidence(project_key: str, context: dict = WRITE) -> dict[str, Any]:
    _require_enabled()
    _require_elevated(context)
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, actor = await _authorize(conn, context, project, "approve")
        return {"requeued": await svc.requeue_waiting_evidence(conn, tenant, actor, project)}


@router.post("/control")
async def control(project_key: str, body: ControlRequest, context: dict = WRITE) -> dict[str, Any]:
    _require_elevated(context)
    project = _project(project_key)
    if body.project_key != project:
        raise HTTPException(422, {"error": "project_key_mismatch"})
    async with get_pool().acquire() as conn:
        tenant, actor = await _authorize(conn, context, project, "approve")
        try:
            return await svc.set_control(conn, tenant, actor, body)
        except svc.MainChatError as exc:
            raise _http(exc) from exc


@router.get("/grants")
async def grants(project_key: str, context: dict = VIEW) -> list[dict[str, Any]]:
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        return await svc.list_grants(conn, tenant, project)


@router.post("/grants", status_code=201)
async def post_grant(project_key: str, body: GrantCreate, context: dict = WRITE) -> dict[str, Any]:
    _require_enabled()
    _require_elevated(context)
    project = _project(project_key)
    if body.project_key != project:
        raise HTTPException(422, {"error": "project_key_mismatch"})
    async with get_pool().acquire() as conn:
        tenant, actor = await _authorize(conn, context, project, "approve")
        try:
            return await svc.create_grant(conn, tenant, actor, body)
        except svc.MainChatError as exc:
            raise _http(exc) from exc


@router.post("/grants/{grant_id}/revoke")
async def revoke(project_key: str, grant_id: UUID, body: GrantRevoke, context: dict = WRITE) -> dict[str, Any]:
    _require_elevated(context)
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, actor = await _authorize(conn, context, project, "approve")
        try:
            return await svc.revoke_grant(conn, tenant, actor, str(grant_id), body.reason)
        except svc.MainChatError as exc:
            raise _http(exc) from exc


@router.get("/notices")
async def notices(project_key: str, unread_only: bool = True, context: dict = VIEW) -> list[dict[str, Any]]:
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, actor = await _authorize(conn, context, project, "read")
        return [n for n in await svc.list_notices(conn, tenant, actor, unread_only) if n["project_key"] == project]


@router.post("/notices/{notice_id}/read")
async def read_notice(project_key: str, notice_id: int, context: dict = VIEW) -> dict[str, bool]:
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, actor = await _authorize(conn, context, project, "read")
        return {"read": await svc.mark_notice_read(conn, tenant, actor, notice_id)}
