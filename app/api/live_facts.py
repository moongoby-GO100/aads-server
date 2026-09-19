"""Tenant-scoped Live Fact observation and final-display revalidation API."""
# ruff: noqa: B008  # FastAPI dependency injection intentionally uses default Depends().
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.services.channel_router import (
    ChannelRouter,
    ObservationEnvelope,
    directive_from_authenticated_context,
    payload_hash,
)
from app.services.live_fact_gate import (
    LiveFactError,
    guard_payload_for_display,
    hash_account_context,
    record_live_fact,
    revalidate_live_fact,
)

router = APIRouter(prefix="/live-facts", tags=["live-facts"])
TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)
require_member = require_tenant_role(TenantRole.MEMBER)


class LiveFactObservationIn(BaseModel):
    session_id: str | None = None
    task_id: str | None = None
    fact_type: str = Field(min_length=1, max_length=80)
    entity_key: str = Field(min_length=1, max_length=300)
    variant_key: str = Field(default="", max_length=300)
    account_context: str = Field(default="", max_length=500, json_schema_extra={"writeOnly": True})
    source_url: str = Field(min_length=1, max_length=2000)
    source_kind: str = Field(min_length=1, max_length=80)
    revalidator_key: str = Field(min_length=1, max_length=120)
    observed_value: Any
    observed_at: datetime
    expires_at: datetime
    evidence_id: str = Field(min_length=1, max_length=500)
    evidence: dict[str, Any] = Field(default_factory=dict)


class FactDisplayIn(BaseModel):
    entity_key: str = Field(default="", max_length=300)
    variant_key: str = Field(default="", max_length=300)
    account_context: str = Field(default="", max_length=500, json_schema_extra={"writeOnly": True})
    force: bool = True


class PayloadDisplayIn(BaseModel):
    payload: dict[str, Any]
    entity_key: str = Field(default="", max_length=300)
    variant_key: str = Field(default="", max_length=300)
    account_context: str = Field(default="", max_length=500, json_schema_extra={"writeOnly": True})
    force: bool = True


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _session_id(request: Request, fallback: str | None = None) -> str:
    return str(fallback or request.headers.get("x-aads-chat-session-id") or "live-fact-api")


def _expected(body: FactDisplayIn | PayloadDisplayIn) -> dict[str, str]:
    return {
        "entity_key": body.entity_key,
        "variant_key": body.variant_key,
        "account_context_hash": hash_account_context(body.account_context),
    }


@router.post("", status_code=201)
async def create_live_fact_observation(
    body: LiveFactObservationIn,
    request: Request,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    payload = body.model_dump(exclude={"account_context", "observed_value", "evidence"})
    payload["value_hash"] = payload_hash({"value": body.observed_value})
    ChannelRouter().route_observation(ObservationEnvelope(
        source=body.source_kind,
        tenant_id=_tenant_id(context),
        session_id=_session_id(request, body.session_id),
        correlation_id=f"live-fact-observe:{body.evidence_id}",
        trust_level="untrusted",
        payload=payload,
        payload_hash=payload_hash(payload),
    ))
    try:
        return await record_live_fact(
            tenant_id=_tenant_id(context), session_id=body.session_id, task_id=body.task_id,
            fact_type=body.fact_type, entity_key=body.entity_key, variant_key=body.variant_key,
            account_context_hash=hash_account_context(body.account_context), source_url=body.source_url,
            source_kind=body.source_kind, revalidator_key=body.revalidator_key,
            observed_value=body.observed_value, observed_at=body.observed_at,
            expires_at=body.expires_at, evidence_id=body.evidence_id, evidence=body.evidence,
        )
    except LiveFactError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc


@router.post("/{fact_id}/display")
async def display_live_fact(
    fact_id: str,
    body: FactDisplayIn,
    request: Request,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    intent_payload = {"fact_id": fact_id, "force": body.force}
    ChannelRouter().route_directive(
        directive_from_authenticated_context(
            context, session_id=_session_id(request), correlation_id=f"live-fact-display:{fact_id}",
            payload=intent_payload, capabilities=frozenset({"browser.fact.revalidate"}),
        ),
        capability="browser.fact.revalidate",
    )
    try:
        return await revalidate_live_fact(
            tenant_id=_tenant_id(context), fact_id=fact_id,
            expected_context=_expected(body), force=body.force,
        )
    except LiveFactError as exc:
        status = 404 if exc.code == "FACT_NOT_FOUND" else 422
        raise HTTPException(status_code=status, detail=exc.code) from exc


@router.post("/display-payload")
async def display_live_fact_payload(
    body: PayloadDisplayIn,
    request: Request,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    intent_payload = {"fact_ids": sorted(str(item) for item in body.payload.get("live_fact_ids", []))}
    ChannelRouter().route_directive(
        directive_from_authenticated_context(
            context, session_id=_session_id(request), correlation_id="live-fact-payload-display",
            payload=intent_payload, capabilities=frozenset({"browser.fact.revalidate"}),
        ),
        capability="browser.fact.revalidate",
    )
    return await guard_payload_for_display(
        body.payload, tenant_id=_tenant_id(context),
        expected_context=_expected(body), force=body.force,
    )
