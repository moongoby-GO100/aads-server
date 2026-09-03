"""Authenticated Site Collector SaaS API."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth import TenantRole, require_tenant_role
from app.services.authenticated_site_collector import (
    create_job, list_jobs, list_site_profiles, overview, resume_job, upsert_site_profile,
)
from app.services.browser_recipe_registry import build_recipe_dry_run_plan, list_browser_recipes, upsert_browser_recipe

router = APIRouter(prefix="/authenticated-site-collector", tags=["authenticated-site-collector"])
TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)
require_member = require_tenant_role(TenantRole.MEMBER)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _user_id(context: TenantContext) -> str:
    return str(context["membership"]["user_id"])


class SiteProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_key: Literal["AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "CUSTOM"]
    site_key: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=200)
    base_origin: str = Field(max_length=2000)
    allowed_origins: list[str] = Field(default_factory=list)
    runtime: Literal["webview2", "chrome_extension", "chrome_cdp", "playwright_server", "file_upload", "official_api", "manual_export"]
    data_categories: list[str] = Field(default_factory=list)
    login_mode: str = Field(default="user_session", max_length=80)
    challenge_policy: dict[str, Any] = Field(default_factory=lambda: {"mode": "user_intervention_only"})
    retention_policy: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class CollectorRecipeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipe_id: str = Field(min_length=1, max_length=200)
    version: str = Field(default="v1", max_length=80)
    title: str = Field(default="", max_length=300)
    project_key: Literal["AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "CUSTOM"]
    service: str = Field(min_length=1, max_length=120, description="site_key")
    site_environment: Literal["webview2", "chrome_extension", "chrome_cdp", "playwright_server", "file_upload", "official_api", "manual_export"]
    record_types: list[str] = Field(default_factory=list)
    normalization_schema: dict[str, Any] = Field(default_factory=dict)
    fixture_cases: list[dict[str, Any]] = Field(default_factory=list)
    version_status: Literal["draft", "active", "archived"] = "draft"
    allowed_origins: list[str] = Field(default_factory=list)
    work_key_template: str = Field(default="", max_length=120)
    runtime_policy: dict[str, Any] = Field(default_factory=dict)
    concurrency_policy: dict[str, Any] = Field(default_factory=dict)
    resource_policy: dict[str, Any] = Field(default_factory=dict)
    login_steps: list[dict[str, Any]] = Field(default_factory=list)
    challenge_policy: dict[str, Any] = Field(default_factory=lambda: {"mode": "user_intervention_only"})
    navigation_steps: list[dict[str, Any]] = Field(default_factory=list)
    capture_rules: dict[str, Any] = Field(default_factory=dict)
    parser_id: str = Field(default="", max_length=200)
    upload_rules: dict[str, Any] = Field(default_factory=dict)
    risk_actions: list[dict[str, Any]] = Field(default_factory=list)
    verifier: dict[str, Any] = Field(default_factory=dict)
    fallbacks: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class DryRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_url: str = Field(default="", max_length=2000)


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_key: str
    site_key: str
    recipe_id: str
    recipe_version: str = "v1"
    work_key: str = Field(default="", max_length=120)
    priority: int = Field(default=50, ge=0, le=1000)
    latest_only: bool = False


class ResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Literal["completed", "relogin_completed", "terms_accepted", "permission_granted", "manual_fallback"]
    note: str = Field(default="", max_length=500)


@router.get("/overview")
async def get_overview(context: TenantContext = Depends(require_viewer)) -> dict[str, Any]:
    return await overview(_tenant_id(context))


@router.get("/site-profiles")
async def get_sites(project_key: str | None = None, enabled: bool | None = None, context: TenantContext = Depends(require_viewer)) -> dict[str, Any]:
    sites = await list_site_profiles(_tenant_id(context), project_key, enabled)
    return {"sites": sites, "count": len(sites), "demo": False}


@router.post("/site-profiles")
async def save_site(body: SiteProfileIn, context: TenantContext = Depends(require_member)) -> dict[str, Any]:
    try:
        site = await upsert_site_profile(_tenant_id(context), _user_id(context), body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "saved", "site": site}


@router.get("/recipes")
async def get_recipes(project_key: str | None = None, site_key: str | None = None, enabled: bool | None = None, context: TenantContext = Depends(require_viewer)) -> dict[str, Any]:
    recipes = await list_browser_recipes(tenant_id=_tenant_id(context), service=site_key, enabled=enabled, project_key=project_key)
    return {"recipes": recipes, "count": len(recipes), "demo": False}


@router.post("/recipes")
async def save_recipe(body: CollectorRecipeIn, context: TenantContext = Depends(require_member)) -> dict[str, Any]:
    try:
        payload = body.model_dump()
        challenge_mode = str(payload["challenge_policy"].get("mode") or "user_intervention_only")
        if challenge_mode != "user_intervention_only":
            raise ValueError("challenge_policy_must_require_user_intervention")
        recipe = await upsert_browser_recipe(tenant_id=_tenant_id(context), user_id=_user_id(context), payload=payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "saved", "recipe": recipe}


@router.post("/recipes/{recipe_id}/dry-run")
async def dry_run(recipe_id: str, body: DryRunIn, version: str = "v1", context: TenantContext = Depends(require_viewer)) -> dict[str, Any]:
    recipes = await list_browser_recipes(tenant_id=_tenant_id(context), enabled=None)
    recipe = next((item for item in recipes if item["recipe_id"] == recipe_id and item["version"] == version), None)
    if not recipe:
        raise HTTPException(status_code=404, detail="collector_recipe_not_found")
    try:
        plan = build_recipe_dry_run_plan(recipe, target_url=body.target_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    fixture_cases = recipe.get("fixture_cases") or []
    plan["fixture_validation"] = {"total": len(fixture_cases), "configured": bool(fixture_cases)}
    plan["challenge_handling"] = "user_intervention_only"
    return {"status": "ok", "plan": plan}


@router.post("/jobs")
async def enqueue_job(body: JobIn, context: TenantContext = Depends(require_member)) -> dict[str, Any]:
    try:
        job = await create_job(_tenant_id(context), _user_id(context), body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "queued", "job": job}


@router.get("/jobs")
async def get_jobs(project_key: str | None = None, status: str | None = None, limit: int = Query(default=100, ge=1, le=200), context: TenantContext = Depends(require_viewer)) -> dict[str, Any]:
    try:
        jobs = await list_jobs(_tenant_id(context), project_key, status, limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"jobs": jobs, "count": len(jobs), "demo": False}


@router.post("/jobs/{job_id}/resume")
async def resume(job_id: str, body: ResumeIn, context: TenantContext = Depends(require_member)) -> dict[str, Any]:
    try:
        job = await resume_job(_tenant_id(context), _user_id(context), job_id, body.resolution, body.note)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not job:
        raise HTTPException(status_code=409, detail="job_not_action_required_or_not_found")
    return {"status": "queued", "same_work_key": True, "job": job}
