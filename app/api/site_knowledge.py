"""Tenant-scoped Smart Browser knowledge, learning, revisit, and skill APIs."""
# ruff: noqa: B008
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import TenantRole, require_tenant_role
from app.services.channel_router import (
    ChannelRouter,
    ObservationEnvelope,
    directive_from_authenticated_context,
    payload_hash,
)
from app.services.live_fact_gate import guard_payload_for_display
from app.services.ohvis_harness import SkillRegistryError, execute_skill
from app.services.site_knowledge import (
    SiteKnowledgeError,
    attach_browser_recipe_provenance,
    attach_site_skill_provenance,
    create_page_template_candidate,
    record_live_observation,
    record_semantic_memory,
)
from app.services.smart_browser_learning import (
    SmartBrowserLearningError,
    assess_page_revisit,
    auto_learn_site_visit,
    index_site_skill_embedding,
    learn_page_template,
    plan_skill_runtime,
    record_runtime_decision,
    resolve_site_skill,
)

router = APIRouter(prefix="/site-knowledge", tags=["site-knowledge"])
TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)
require_member = require_tenant_role(TenantRole.MEMBER)
require_admin = require_tenant_role(TenantRole.ADMIN)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageTemplateIn(_Strict):
    page_key: str = Field(min_length=1, max_length=300)
    version: str = Field(min_length=1, max_length=120)
    template: dict[str, Any]
    evidence_refs: list[str] = Field(min_length=1, max_length=50)
    expires_at: datetime
    provenance: dict[str, Any] = Field(default_factory=dict)


class PageLearningIn(_Strict):
    page_key: str = Field(min_length=1, max_length=300)
    version: str = Field(min_length=1, max_length=120)
    area_key: str = Field(min_length=1, max_length=120)
    aria_nodes: list[dict[str, Any]] = Field(min_length=1, max_length=5000)
    template_contract: dict[str, Any]
    evidence_refs: list[str] = Field(min_length=1, max_length=50)
    expires_at: datetime
    provenance: dict[str, Any] = Field(default_factory=dict)


class PageRevisitIn(_Strict):
    page_key: str = Field(min_length=1, max_length=300)
    area_key: str = Field(min_length=1, max_length=120)
    aria_nodes: list[dict[str, Any]] = Field(max_length=5000)


class AutoSiteVisitIn(_Strict):
    page_key: str = Field(min_length=1, max_length=300)
    area_key: str = Field(min_length=1, max_length=120)
    aria_nodes: list[dict[str, Any]] = Field(min_length=1, max_length=5000)
    template_contract: dict[str, Any]
    evidence_refs: list[str] = Field(min_length=1, max_length=50)
    expires_at: datetime


class SemanticMemoryIn(_Strict):
    project: str = Field(min_length=1, max_length=20)
    category: str = Field(min_length=1, max_length=30)
    subject: str = Field(min_length=1, max_length=300)
    detail: str = Field(min_length=1, max_length=12000)
    evidence_refs: list[str] = Field(min_length=1, max_length=50)
    expires_at: datetime
    provenance: dict[str, Any] = Field(default_factory=dict)


class LiveObservationIn(_Strict):
    fact_type: str = Field(min_length=1, max_length=80)
    entity_key: str = Field(min_length=1, max_length=300)
    variant_key: str = Field(default="", max_length=300)
    account_context: str = Field(
        default="", max_length=500, json_schema_extra={"writeOnly": True},
    )
    source_url: str = Field(min_length=1, max_length=2000)
    revalidator_key: str = Field(min_length=1, max_length=120)
    observed_value: Any
    observed_at: datetime
    expires_at: datetime
    evidence_id: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=50)
    provenance: dict[str, Any] = Field(default_factory=dict)


class SiteSkillProvenanceIn(_Strict):
    evidence_refs: list[str] = Field(min_length=1, max_length=50)
    expires_at: datetime
    provenance: dict[str, Any] = Field(default_factory=dict)


class BrowserRecipeProvenanceIn(_Strict):
    expires_at: datetime
    provenance: dict[str, Any] = Field(default_factory=dict)


class SkillResolveIn(_Strict):
    query: str = Field(min_length=1, max_length=500)
    allow_llm_fallback: bool = True
    required_capabilities: list[str] = Field(default_factory=list, max_length=20)
    max_llm_cost_usd: float = Field(default=0.01, ge=0, le=0.01)


class SkillExecuteIn(SkillResolveIn):
    input: dict[str, Any]
    idempotency_key: str | None = Field(default=None, max_length=200)
    approval_id: str | None = None
    session_id: str = Field(min_length=1, max_length=200)
    correlation_id: str = Field(min_length=1, max_length=200)
    session_available: bool = False
    local_environment_available: bool = False


def _tenant(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _route_page_observation(
    *, context: TenantContext, request: Request, correlation_id: str,
    metadata: dict[str, Any], source: str = "aria",
) -> None:
    """Bind page-derived data to the untrusted observation channel."""
    ChannelRouter().route_observation(ObservationEnvelope(
        source=source, tenant_id=_tenant(context),
        session_id=str(request.headers.get("x-aads-chat-session-id") or "site-knowledge-api"),
        correlation_id=correlation_id, trust_level="untrusted",
        payload=metadata, payload_hash=payload_hash(metadata),
    ))


def _error(exc: Exception) -> None:
    detail = str(exc)
    not_found = {
        "site_profile_not_found", "browser_recipe_not_found",
        "site_skill_version_not_found", "active_site_skill_not_found",
    }
    raise HTTPException(status_code=404 if detail in not_found else 422, detail=detail) from exc


@router.post("/profiles/{site_profile_id}/page-templates", status_code=201)
async def create_page_template(
    site_profile_id: str, body: PageTemplateIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        payload = body.model_dump()
        payload["evidence"] = payload.pop("evidence_refs")
        return await create_page_template_candidate(
            tenant_id=_tenant(context), site_profile_id=site_profile_id, **payload,
        )
    except SiteKnowledgeError as exc:
        _error(exc)


@router.post("/profiles/{site_profile_id}/learn", status_code=201)
async def learn_page(
    site_profile_id: str, body: PageLearningIn, request: Request,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        _route_page_observation(
            context=context, request=request,
            correlation_id=f"site-learn:{site_profile_id}:{body.page_key}",
            metadata={"site_profile_id": site_profile_id, "page_key": body.page_key,
                      "node_count": len(body.aria_nodes)},
        )
        return await learn_page_template(
            tenant_id=_tenant(context), site_profile_id=site_profile_id,
            **body.model_dump(),
        )
    except (SiteKnowledgeError, SmartBrowserLearningError) as exc:
        _error(exc)


@router.post("/profiles/{site_profile_id}/revisit")
async def revisit_page(
    site_profile_id: str, body: PageRevisitIn, request: Request,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    try:
        _route_page_observation(
            context=context, request=request,
            correlation_id=f"site-revisit:{site_profile_id}:{body.page_key}",
            metadata={"site_profile_id": site_profile_id, "page_key": body.page_key,
                      "node_count": len(body.aria_nodes)},
        )
        return await assess_page_revisit(
            tenant_id=_tenant(context), site_profile_id=site_profile_id,
            **body.model_dump(),
        )
    except (SiteKnowledgeError, SmartBrowserLearningError) as exc:
        _error(exc)


@router.post("/profiles/{site_profile_id}/auto-visit", status_code=201)
async def auto_site_visit(
    site_profile_id: str, body: AutoSiteVisitIn, request: Request,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    """Create or reuse paired candidate artifacts without executing page data."""
    try:
        _route_page_observation(
            context=context, request=request,
            correlation_id=f"site-auto-visit:{site_profile_id}:{body.page_key}",
            metadata={"site_profile_id": site_profile_id, "page_key": body.page_key,
                      "node_count": len(body.aria_nodes)},
        )
        payload = body.model_dump()
        payload["evidence"] = payload.pop("evidence_refs")
        return await auto_learn_site_visit(
            tenant_id=_tenant(context), site_profile_id=site_profile_id, **payload,
        )
    except (SiteKnowledgeError, SmartBrowserLearningError) as exc:
        _error(exc)


@router.put("/profiles/{site_profile_id}/browser-recipes/{recipe_id}/versions/{version}/provenance")
async def attach_browser_recipe(
    site_profile_id: str, recipe_id: str, version: str,
    body: BrowserRecipeProvenanceIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        return await attach_browser_recipe_provenance(
            tenant_id=_tenant(context), site_profile_id=site_profile_id,
            recipe_id=recipe_id, version=version, **body.model_dump(),
        )
    except SiteKnowledgeError as exc:
        _error(exc)


@router.post("/profiles/{site_profile_id}/semantic-memory", status_code=201)
async def create_semantic_memory(
    site_profile_id: str, body: SemanticMemoryIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        payload = body.model_dump()
        payload["evidence"] = payload.pop("evidence_refs")
        return await record_semantic_memory(
            tenant_id=_tenant(context), site_profile_id=site_profile_id, **payload,
        )
    except SiteKnowledgeError as exc:
        _error(exc)


@router.put("/profiles/{site_profile_id}/skills/{skill_id}/versions/{version}/provenance")
async def attach_site_skill(
    site_profile_id: str, skill_id: str, version: str,
    body: SiteSkillProvenanceIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        payload = body.model_dump()
        payload["evidence"] = payload.pop("evidence_refs")
        return await attach_site_skill_provenance(
            tenant_id=_tenant(context), site_profile_id=site_profile_id,
            skill_id=skill_id, version=version, **payload,
        )
    except SiteKnowledgeError as exc:
        _error(exc)


@router.post("/profiles/{site_profile_id}/skills/{skill_id}/versions/{version}/index")
async def index_site_skill(
    site_profile_id: str, skill_id: str, version: str,
    context: TenantContext = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return await index_site_skill_embedding(
            tenant_id=_tenant(context), site_profile_id=site_profile_id,
            skill_id=skill_id, version=version,
        )
    except SmartBrowserLearningError as exc:
        _error(exc)


@router.post("/profiles/{site_profile_id}/skill-search")
async def search_site_skill(
    site_profile_id: str, body: SkillResolveIn,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    try:
        return await resolve_site_skill(
            tenant_id=_tenant(context), site_profile_id=site_profile_id,
            **body.model_dump(),
        )
    except SmartBrowserLearningError as exc:
        _error(exc)


@router.post("/profiles/{site_profile_id}/skill-execute")
async def execute_site_skill(
    site_profile_id: str, body: SkillExecuteIn, request: Request,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        selected = await resolve_site_skill(
            tenant_id=_tenant(context), site_profile_id=site_profile_id,
            query=body.query, allow_llm_fallback=body.allow_llm_fallback,
            required_capabilities=body.required_capabilities,
            max_llm_cost_usd=body.max_llm_cost_usd,
        )
        membership = context.get("membership") or {}
        authenticated_permissions = membership.get("permissions") or []
        contract = selected.get("execution_contract") or {}
        contract_capabilities = set(contract.get("capabilities") or [])
        safety_contract_match = bool(contract.get("executor")) and set(
            body.required_capabilities
        ).issubset(contract_capabilities)
        runtime = plan_skill_runtime(
            selected=selected, required_capabilities=body.required_capabilities,
            permissions=authenticated_permissions, session_available=body.session_available,
            local_environment_available=body.local_environment_available,
            safety_contract_match=safety_contract_match,
        )
        await record_runtime_decision(
            tenant_id=_tenant(context), site_profile_id=site_profile_id,
            selected=selected, runtime=runtime,
        )
        if not runtime["executable"]:
            return {"selection": selected, "runtime": runtime, "run": None,
                    "request_id": request.headers.get("x-request-id")}
        payload = {
            "skill_id": selected["skill_id"], "version": selected["version"],
            "input": body.input,
        }
        envelope = directive_from_authenticated_context(
            context, session_id=body.session_id,
            correlation_id=body.correlation_id,
            payload=payload, capabilities=frozenset({"skill.execute"}),
        )
        intent = ChannelRouter().route_directive(envelope, capability="skill.execute")
        result = await execute_skill(
            tenant_id=_tenant(context), skill_id=selected["skill_id"],
            version=selected["version"], input_data=body.input,
            idempotency_key=body.idempotency_key, action_intent=intent,
            approval_id=body.approval_id,
        )
        raw_output = result.get("output") or {}
        if isinstance(raw_output, str):
            raw_output = json.loads(raw_output)
        display = await guard_payload_for_display(
            raw_output if isinstance(raw_output, dict) else {"result": raw_output},
            tenant_id=_tenant(context), force=True,
        )
        safe_run = dict(result)
        safe_run.pop("output", None)
        return {"selection": selected, "runtime": runtime, "run": safe_run, "display": display,
                "request_id": request.headers.get("x-request-id")}
    except (SmartBrowserLearningError, SkillRegistryError) as exc:
        status = exc.status_code if isinstance(exc, SkillRegistryError) else 422
        detail = exc.code if isinstance(exc, SkillRegistryError) else str(exc)
        raise HTTPException(status_code=status, detail=detail) from exc


@router.post("/profiles/{site_profile_id}/live-observations", status_code=201)
async def validate_live_observation(
    site_profile_id: str, body: LiveObservationIn, request: Request,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        _route_page_observation(
            context=context, request=request,
            correlation_id=f"site-live-observation:{site_profile_id}:{body.evidence_id}",
            source="page_text",
            metadata={"site_profile_id": site_profile_id, "fact_type": body.fact_type,
                      "entity_key": body.entity_key,
                      "value_hash": payload_hash({"value": body.observed_value})},
        )
        payload = body.model_dump()
        payload["evidence"] = payload.pop("evidence_refs")
        return await record_live_observation(
            tenant_id=_tenant(context), site_profile_id=site_profile_id, **payload,
        )
    except SiteKnowledgeError as exc:
        _error(exc)
