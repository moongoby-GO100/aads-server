"""R-DOC mockup review API. Routes only: auth mapping, transactions and status codes live here."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import JSONResponse

from app.api.canonical_documents import _authorize, _project, _scope
from app.auth import TenantRole, require_tenant_role
from app.core.db_pool import get_pool
from app.models.mockup_review import (
    ApproveReview, ChangeCreate, ReviewCreate, RevisingStart, RevisionCreate, RevokeReview, SubmitReview,
    VerifyBundle,
)
from app.services import mockup_review_service as svc

router = APIRouter(prefix="/projects/{project_key}/mockup-reviews", tags=["mockup-reviews"])
VIEW = Depends(require_tenant_role(TenantRole.VIEWER))
WRITE = Depends(require_tenant_role(TenantRole.MEMBER))
ASSET_ID = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


async def _run(project_key: str, context: dict, access: str, work: Any) -> Any:
    project = _project(project_key)
    async with get_pool().acquire() as conn, conn.transaction():
        tenant, actor = await _authorize(conn, context, project, access)
        elevated = _scope(context)[2]
        return await work(conn, tenant, project, actor, elevated)


@router.post("", status_code=201)
async def create_review(project_key: str, body: ReviewCreate, response: Response, context: dict = WRITE):
    result = await _run(project_key, context, "write",
                        lambda c, t, p, a, e: svc.create_review(c, t, p, a, e, body))
    if result.get("idempotent"):
        response.status_code = 200
    return result


@router.get("/{review_id}")
async def get_review(project_key: str, review_id: UUID, context: dict = VIEW):
    return await _run(project_key, context, "read", lambda c, t, p, a, e: svc.get_review(c, t, p, review_id))


@router.post("/{review_id}/revisions", status_code=201)
async def create_revision(project_key: str, review_id: UUID, body: RevisionCreate, response: Response,
                          context: dict = WRITE):
    result = await _run(project_key, context, "write",
                        lambda c, t, p, a, e: svc.create_revision(c, t, p, review_id, a, body))
    if result.get("idempotent"):
        response.status_code = 200
    elif result.get("pointer_applied") is False:
        response.status_code = 202
    return result


@router.get("/{review_id}/revisions/{revision_id}")
async def get_revision(project_key: str, review_id: UUID, revision_id: UUID, context: dict = VIEW):
    return await _run(project_key, context, "read",
                      lambda c, t, p, a, e: svc.get_revision(c, t, p, review_id, revision_id))


@router.get("/{review_id}/revisions/{revision_id}/assets/{asset_id}")
async def get_asset(project_key: str, review_id: UUID, revision_id: UUID, asset_id: str = ASSET_ID,
                    context: dict = VIEW):
    data, mime = await _run(project_key, context, "read",
                            lambda c, t, p, a, e: svc.get_asset(c, t, p, review_id, revision_id, asset_id))
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=31536000, immutable",
               "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"}
    return Response(content=data, media_type=mime, headers=headers)


@router.post("/{review_id}/revising")
async def start_revising(project_key: str, review_id: UUID, body: RevisingStart, context: dict = WRITE):
    return await _run(project_key, context, "write",
                      lambda c, t, p, a, e: svc.start_revising(c, t, p, review_id, a, body))


@router.post("/{review_id}/submit")
async def submit_review(project_key: str, review_id: UUID, body: SubmitReview, context: dict = WRITE):
    return await _run(project_key, context, "write",
                      lambda c, t, p, a, e: svc.submit_review(c, t, p, review_id, a, body))


@router.post("/{review_id}/changes", status_code=201)
async def request_changes(project_key: str, review_id: UUID, body: ChangeCreate, response: Response,
                          context: dict = WRITE):
    result = await _run(project_key, context, "write",
                        lambda c, t, p, a, e: svc.request_changes(c, t, p, review_id, a, e, body))
    if result.get("idempotent"):
        response.status_code = 200
    return result


@router.get("/{review_id}/change-report")
async def change_report(project_key: str, review_id: UUID, context: dict = VIEW):
    return await _run(project_key, context, "read", lambda c, t, p, a, e: svc.change_report(c, t, p, review_id))


@router.post("/{review_id}/approve")
async def approve_review(project_key: str, review_id: UUID, body: ApproveReview, context: dict = WRITE):
    return await _run(project_key, context, "approve",
                      lambda c, t, p, a, e: svc.approve_review(c, t, p, review_id, a, body))


@router.post("/{review_id}/revoke")
async def revoke_review(project_key: str, review_id: UUID, body: RevokeReview, context: dict = WRITE):
    return await _run(project_key, context, "approve",
                      lambda c, t, p, a, e: svc.revoke_review(c, t, p, review_id, a, body))


@router.post("/{review_id}/verify")
async def verify_bundle(project_key: str, review_id: UUID, body: VerifyBundle, context: dict = WRITE):
    result = await _run(project_key, context, "write",
                        lambda c, t, p, a, e: svc.verify_bundle(c, t, p, review_id, a, body))
    if not result["allowed"]:
        # Answered after the transaction commits so the verify_denied audit row survives.
        return JSONResponse(status_code=409, content={"detail": result})
    return result
