"""Authenticated site collector SaaS API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.services.authenticated_site_collector import (
    SITE_ENVIRONMENTS,
    build_collector_recipe_dry_run,
    collector_overview,
    create_collection_job,
    list_jobs,
    list_site_profiles,
    normalize_recipe_extension,
    resume_collection_job,
    upsert_site_profile,
)

router = APIRouter(prefix="/authenticated-site-collector", tags=["authenticated-site-collector"])
TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)
require_member = require_tenant_role(TenantRole.MEMBER)


class SiteProfileIn(BaseModel):
    project_key: str = Field(default="CUSTOM", max_length=40)
    site_key: str = Field(min_length=1, max_length=120)
    display_name: str = Field(default="", max_length=200)
    base_origin: str = Field(default="", max_length=2000)
    allowed_origins: list[str] = Field(default_factory=list)
    runtime: str = Field(default="webview2", max_length=80)
    data_categories: list[str] = Field(default_factory=list)
    login_mode: str = Field(default="user_session", max_length=80)
    challenge_policy: str = Field(default="user_intervention", max_length=80)
    retention_policy: dict[str, Any] = Field(default_factory=dict)
    account_count: int = Field(default=0, ge=0, le=100000)
    connected_account_count: int = Field(default=0, ge=0, le=100000)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class CollectorRecipeIn(BaseModel):
    recipe_id: str = Field(min_length=1, max_length=200)
    version: str = Field(default="v1", min_length=1, max_length=80)
    title: str = Field(default="", max_length=300)
    service: str = Field(default="", max_length=120)
    allowed_origins: list[str] = Field(default_factory=list)
    work_key_template: str = Field(default="", max_length=120)
    runtime_policy: dict[str, Any] = Field(default_factory=dict)
    concurrency_policy: dict[str, Any] = Field(default_factory=dict)
    resource_policy: dict[str, Any] = Field(default_factory=dict)
    login_steps: list[dict[str, Any]] = Field(default_factory=list)
    challenge_policy: dict[str, Any] = Field(default_factory=dict)
    navigation_steps: list[dict[str, Any]] = Field(default_factory=list)
    capture_rules: dict[str, Any] = Field(default_factory=dict)
    parser_id: str = Field(default="", max_length=200)
    upload_rules: dict[str, Any] = Field(default_factory=dict)
    risk_actions: list[dict[str, Any]] = Field(default_factory=list)
    verifier: dict[str, Any] = Field(default_factory=dict)
    fallbacks: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    project_key: str = Field(default="CUSTOM", max_length=40)
    site_environment: str = Field(default="webview2", max_length=80)
    record_types: list[str] = Field(default_factory=list)
    normalization_schema: dict[str, Any] = Field(default_factory=dict)
    fixture_cases: list[dict[str, Any]] = Field(default_factory=list)
    version_status: str = Field(default="draft", max_length=40)


class CollectorRecipeDryRunIn(BaseModel):
    target_url: str = Field(default="", max_length=2000)


class CollectorJobIn(BaseModel):
    project_key: str = Field(default="CUSTOM", max_length=40)
    site_key: str = Field(min_length=1, max_length=120)
    recipe_id: str = Field(min_length=1, max_length=200)
    recipe_version: str = Field(default="v1", max_length=80)
    work_key: str = Field(default="", max_length=120)
    priority: int = Field(default=50, ge=0, le=1000)
    latest_only: bool = True


class CollectorResumeIn(BaseModel):
    resolution: str = Field(default="completed", max_length=80)
    note: str = Field(default="", max_length=1000)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _user_id(context: TenantContext) -> str:
    return str(context["membership"]["user_id"])


@router.get("/overview")
async def api_collector_overview(
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    return await collector_overview(tenant_id=_tenant_id(context))


@router.get("/site-profiles")
async def api_list_site_profiles(
    project_key: str | None = Query(default=None, max_length=40),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    return await list_site_profiles(tenant_id=_tenant_id(context), project_key=project_key)


@router.post("/site-profiles")
async def api_upsert_site_profile(
    body: SiteProfileIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    profile = await upsert_site_profile(
        tenant_id=_tenant_id(context),
        user_id=_user_id(context),
        payload=body.model_dump(),
    )
    return {"status": "saved", "site": profile}


@router.get("/recipes")
async def api_list_collector_recipes(
    project_key: str | None = Query(default=None, max_length=40),
    site_environment: str | None = Query(default=None, max_length=80),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    profiles = await list_site_profiles(tenant_id=_tenant_id(context), project_key=project_key)
    recipes = []
    for profile in profiles["sites"]:
        if site_environment and profile["runtime"] != site_environment:
            continue
        recipes.append(
            {
                "recipe_id": f"{profile['site_key']}.collect",
                "version": "v1",
                "title": f"{profile['display_name']} collection",
                "project_key": profile["project_key"],
                "site_key": profile["site_key"],
                "site_environment": profile["runtime"],
                "record_types": profile["data_categories"],
                "version_status": "draft" if profile.get("metadata", {}).get("sample") else "active",
                "demo": bool(profile.get("metadata", {}).get("sample")),
            }
        )
    return {"recipes": recipes, "count": len(recipes), "supported_site_environments": sorted(SITE_ENVIRONMENTS)}


@router.post("/recipes")
async def api_validate_collector_recipe(
    body: CollectorRecipeIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    extension = normalize_recipe_extension(body.model_dump())
    return {"status": "validated", "recipe": body.model_dump(), "saas_extension": extension}


@router.post("/recipes/{recipe_id}/dry-run")
async def api_dry_run_collector_recipe(
    recipe_id: str,
    body: CollectorRecipeDryRunIn,
    version: str = Query(default="v1", max_length=80),
    project_key: str = Query(default="CUSTOM", max_length=40),
    site_environment: str = Query(default="webview2", max_length=80),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    recipe = {
        "recipe_id": recipe_id,
        "version": version,
        "allowed_origins": [body.target_url] if body.target_url else ["https://example.com"],
        "work_key_template": recipe_id,
        "resource_policy": {"runtime": "pc_agent" if site_environment == "webview2" else site_environment},
        "risk_actions": [],
    }
    try:
        plan = build_collector_recipe_dry_run(
            recipe,
            target_url=body.target_url,
            project_key=project_key,
            site_environment=site_environment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "plan": plan}


@router.get("/jobs")
async def api_list_collector_jobs(
    project_key: str | None = Query(default=None, max_length=40),
    status: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    return list_jobs(project_key=project_key, status=status, limit=limit)


@router.post("/jobs")
async def api_create_collector_job(
    body: CollectorJobIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        return await create_collection_job(
            tenant_id=_tenant_id(context),
            user_id=_user_id(context),
            payload=body.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/resume")
async def api_resume_collector_job(
    job_id: str,
    body: CollectorResumeIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    result = resume_collection_job(job_id=job_id, resolution=body.resolution, note=body.note)
    if not result:
        raise HTTPException(status_code=404, detail="collector_job_not_found")
    return result
