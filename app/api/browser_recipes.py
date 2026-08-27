"""OHVIS BrowserRecipe registry API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.services.browser_recipe_registry import (
    build_recipe_dry_run_plan,
    create_browser_recipe_run,
    get_browser_recipe,
    list_browser_recipes,
    plan_browser_recipe_run,
    upsert_browser_recipe,
)

router = APIRouter(prefix="/browser-recipes", tags=["browser-recipes"])
TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)
require_member = require_tenant_role(TenantRole.MEMBER)


class BrowserRecipeIn(BaseModel):
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


class BrowserRecipeDryRunIn(BrowserRecipeIn):
    target_url: str = Field(default="", max_length=2000)


class BrowserRecipeRunIn(BaseModel):
    target_url: str = Field(default="", max_length=2000)
    work_key: str = Field(default="", max_length=120)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _user_id(context: TenantContext) -> str:
    return str(context["membership"]["user_id"])


@router.get("")
async def api_list_browser_recipes(
    service: str | None = None,
    enabled: bool | None = Query(default=None),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    recipes = await list_browser_recipes(tenant_id=_tenant_id(context), service=service, enabled=enabled)
    return {"recipes": recipes, "count": len(recipes)}


@router.post("")
async def api_upsert_browser_recipe(
    body: BrowserRecipeIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        recipe = await upsert_browser_recipe(
            tenant_id=_tenant_id(context),
            user_id=_user_id(context),
            payload=body.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "saved", "recipe": recipe}


@router.post("/dry-run")
async def api_dry_run_browser_recipe(
    body: BrowserRecipeDryRunIn,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    try:
        plan = build_recipe_dry_run_plan(body.model_dump(), target_url=body.target_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "plan": plan}


@router.get("/{recipe_id}/versions/{version}")
async def api_get_browser_recipe(
    recipe_id: str,
    version: str,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    recipe = await get_browser_recipe(tenant_id=_tenant_id(context), recipe_id=recipe_id, version=version)
    if not recipe:
        raise HTTPException(status_code=404, detail="browser_recipe_not_found")
    return recipe


@router.post("/{recipe_id}/versions/{version}/run-plan")
async def api_plan_browser_recipe_run(
    recipe_id: str,
    version: str,
    body: BrowserRecipeRunIn,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    plan = await plan_browser_recipe_run(
        tenant_id=_tenant_id(context),
        recipe_id=recipe_id,
        version=version,
        target_url=body.target_url,
        work_key=body.work_key,
    )
    if not plan:
        raise HTTPException(status_code=404, detail="browser_recipe_not_found")
    return {"status": "ok", "plan": plan}


@router.post("/{recipe_id}/versions/{version}/runs")
async def api_create_browser_recipe_run(
    recipe_id: str,
    version: str,
    body: BrowserRecipeRunIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    result = await create_browser_recipe_run(
        tenant_id=_tenant_id(context),
        recipe_id=recipe_id,
        version=version,
        target_url=body.target_url,
        work_key=body.work_key,
    )
    if not result:
        raise HTTPException(status_code=404, detail="browser_recipe_not_found")
    if result.get("status") == "rejected":
        raise HTTPException(status_code=409, detail=result)
    return result
