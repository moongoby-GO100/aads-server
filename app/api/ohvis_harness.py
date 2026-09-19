"""OHVIS harness, Skill Find, LLM Wiki, and Hermes-pattern APIs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth import TenantRole, require_tenant_role
from app.services.channel_router import ChannelRouter, directive_from_authenticated_context
from app.services.ohvis_harness import (
    RISK_POLICIES,
    SkillRegistryError,
    add_skill_version,
    create_skill,
    disable_skill,
    execute_skill,
    find_skills,
    get_harness_status,
    list_skill_manifests,
    promote_skill_version,
    recommend_hermes_improvements,
    search_wiki,
    update_skill,
    validate_stored_skill,
)

router = APIRouter()
TenantContext = dict[str, object]
require_tenant_viewer = require_tenant_role(TenantRole.VIEWER)
require_tenant_member = require_tenant_role(TenantRole.MEMBER)
require_tenant_admin = require_tenant_role(TenantRole.ADMIN)
tenant_viewer_dependency = Depends(require_tenant_viewer)
tenant_member_dependency = Depends(require_tenant_member)
tenant_admin_dependency = Depends(require_tenant_admin)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])  # type: ignore[index]


def _actor(context: TenantContext) -> str:
    user = context.get("user") or {}
    if isinstance(user, dict):
        return str(user.get("email") or user.get("id") or "skill_api")
    return "skill_api"


def _raise_registry_error(exc: SkillRegistryError) -> None:
    raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.detail}) from exc


class SkillCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=3, max_length=120, pattern=r"^[a-z0-9][a-z0-9-]+$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    projects: list[str] = Field(default_factory=list, max_length=30)
    intents: list[str] = Field(default_factory=list, max_length=30)


class SkillUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    projects: list[str] = Field(default_factory=list, max_length=30)
    intents: list[str] = Field(default_factory=list, max_length=30)


class SkillVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: dict[str, Any]


class SkillPromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence: list[str] = Field(min_length=1, max_length=100)


class SkillExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=80)
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)
    approval_id: str | None = None
    session_id: str = Field(min_length=1, max_length=100)
    correlation_id: str = Field(min_length=1, max_length=100)


class SkillFindRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    project: str | None = Field(None, max_length=40)
    intent: str | None = Field(None, max_length=80)
    limit: int = Field(5, ge=1, le=20)


class WikiSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    project: str | None = Field(None, max_length=40)
    limit: int = Field(10, ge=1, le=50)


class HermesRecommendRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    project: str | None = Field(None, max_length=40)
    recent_failure: str | None = Field(None, max_length=2000)


@router.get("/ohvis/harness/status", tags=["ohvis-harness"])
async def ohvis_harness_status(project: str | None = Query(None, max_length=40)):
    return await get_harness_status(project=project)


@router.get("/ohvis/harness/policies", tags=["ohvis-harness"])
async def ohvis_harness_policies():
    return {"risk_policies": RISK_POLICIES}


@router.post("/ohvis/harness/skills", status_code=201, tags=["ohvis-harness"])
async def ohvis_create_skill(
    req: SkillCreateRequest,
    context: TenantContext = tenant_member_dependency,
):
    try:
        return await create_skill(
            tenant_id=_tenant_id(context), actor=_actor(context), **req.model_dump()
        )
    except SkillRegistryError as exc:
        _raise_registry_error(exc)


@router.get("/ohvis/harness/skills", tags=["ohvis-harness"])
async def ohvis_list_skills(
    slug: str | None = Query(None, max_length=120),
    version_status: str | None = Query(None, alias="status", max_length=20),
    context: TenantContext = tenant_viewer_dependency,
):
    return {"items": await list_skill_manifests(
        tenant_id=_tenant_id(context), slug=slug, status=version_status
    )}


@router.put("/ohvis/harness/skills/{skill_id}", tags=["ohvis-harness"])
async def ohvis_update_skill(
    skill_id: str,
    req: SkillUpdateRequest,
    context: TenantContext = tenant_member_dependency,
):
    try:
        return await update_skill(
            tenant_id=_tenant_id(context), skill_id=skill_id, **req.model_dump()
        )
    except SkillRegistryError as exc:
        _raise_registry_error(exc)


@router.delete("/ohvis/harness/skills/{skill_id}", tags=["ohvis-harness"])
async def ohvis_disable_skill(
    skill_id: str,
    context: TenantContext = tenant_admin_dependency,
):
    try:
        return await disable_skill(tenant_id=_tenant_id(context), skill_id=skill_id)
    except SkillRegistryError as exc:
        _raise_registry_error(exc)


@router.post("/ohvis/harness/skills/{skill_id}/versions", status_code=201, tags=["ohvis-harness"])
async def ohvis_add_skill_version(
    skill_id: str,
    req: SkillVersionRequest,
    context: TenantContext = tenant_member_dependency,
):
    try:
        return await add_skill_version(
            tenant_id=_tenant_id(context), skill_id=skill_id, manifest=req.manifest
        )
    except SkillRegistryError as exc:
        _raise_registry_error(exc)


@router.post("/ohvis/harness/skills/{skill_id}/versions/{version}/validate", tags=["ohvis-harness"])
async def ohvis_validate_skill_version(
    skill_id: str,
    version: str,
    context: TenantContext = tenant_viewer_dependency,
):
    try:
        return await validate_stored_skill(
            tenant_id=_tenant_id(context), skill_id=skill_id, version=version
        )
    except SkillRegistryError as exc:
        _raise_registry_error(exc)


@router.post("/ohvis/harness/skills/{skill_id}/versions/{version}/promote", tags=["ohvis-harness"])
async def ohvis_promote_skill_version(
    skill_id: str,
    version: str,
    req: SkillPromoteRequest,
    context: TenantContext = tenant_admin_dependency,
):
    try:
        return await promote_skill_version(
            tenant_id=_tenant_id(context), skill_id=skill_id, version=version,
            actor=_actor(context), evidence=req.evidence,
        )
    except SkillRegistryError as exc:
        _raise_registry_error(exc)


@router.post("/ohvis/harness/skills/{skill_id}/execute", tags=["ohvis-harness"])
async def ohvis_execute_skill(
    skill_id: str,
    req: SkillExecuteRequest,
    context: TenantContext = tenant_member_dependency,
):
    payload = {"skill_id": skill_id, "version": req.version, "input": req.input}
    envelope = directive_from_authenticated_context(
        context, session_id=req.session_id, correlation_id=req.correlation_id,
        payload=payload, capabilities=frozenset({"skill.execute"}),
    )
    intent = ChannelRouter().route_directive(envelope, capability="skill.execute")
    try:
        return await execute_skill(
            tenant_id=_tenant_id(context), skill_id=skill_id, version=req.version,
            input_data=req.input, idempotency_key=req.idempotency_key,
            action_intent=intent, approval_id=req.approval_id,
        )
    except SkillRegistryError as exc:
        _raise_registry_error(exc)


@router.post("/ohvis/harness/skill-find", tags=["ohvis-harness"])
async def ohvis_skill_find(req: SkillFindRequest):
    return await find_skills(
        query=req.query,
        project=req.project,
        intent=req.intent,
        limit=req.limit,
    )


@router.post("/ohvis/harness/wiki/search", tags=["ohvis-harness"])
async def ohvis_wiki_search(req: WikiSearchRequest):
    return await search_wiki(query=req.query, project=req.project, limit=req.limit)


@router.post("/ohvis/harness/hermes/recommend", tags=["ohvis-harness"])
async def ohvis_hermes_recommend(req: HermesRecommendRequest):
    return await recommend_hermes_improvements(
        goal=req.goal,
        project=req.project,
        recent_failure=req.recent_failure,
    )
