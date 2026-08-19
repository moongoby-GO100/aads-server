"""OHVIS Agent Vault API."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import TenantRole, require_tenant_role
from app.services.agent_vault_service import (
    disable_agent_credential,
    issue_autofill_token,
    list_access_logs,
    list_agent_credentials,
    redeem_autofill_token,
    upsert_agent_credential,
)
from app.services.browser_permission_policy import classify_browser_action

router = APIRouter(prefix="/agent-vault", tags=["agent-vault"])
TenantContext = dict[str, Any]
require_viewer = require_tenant_role(TenantRole.VIEWER)
require_member = require_tenant_role(TenantRole.MEMBER)


class CredentialIn(BaseModel):
    work_key: str = Field(min_length=1, max_length=120)
    origin: str = Field(min_length=1, max_length=500)
    label: str = Field(default="default", max_length=120)
    username: str = Field(min_length=1, max_length=500)
    password: str = Field(min_length=1, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AutofillTokenIn(BaseModel):
    credential_id: str
    work_key: str = Field(min_length=1, max_length=120)
    origin: str = Field(min_length=1, max_length=500)
    ttl_seconds: int = Field(default=60, ge=1, le=60)


class AutofillRedeemIn(BaseModel):
    token: str = Field(min_length=16, max_length=256)
    work_key: str = Field(min_length=1, max_length=120)
    origin: str = Field(min_length=1, max_length=500)


class CheckActionIn(BaseModel):
    action_type: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=1000)
    payload: dict[str, Any] = Field(default_factory=dict)


def _tenant_id(context: TenantContext) -> str:
    return str(context["tenant"]["id"])


def _user_id(context: TenantContext) -> str:
    return str(context["membership"]["user_id"])


@router.get("/credentials")
async def api_list_credentials(
    work_key: str | None = None,
    origin: str | None = None,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    credentials = await list_agent_credentials(tenant_id=_tenant_id(context), work_key=work_key, origin=origin)
    return {"credentials": credentials, "count": len(credentials)}


@router.post("/credentials")
async def api_upsert_credential(
    body: CredentialIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    credential = await upsert_agent_credential(
        tenant_id=_tenant_id(context),
        user_id=_user_id(context),
        work_key=body.work_key,
        origin=body.origin,
        label=body.label,
        username=body.username,
        password=body.password,
        metadata=body.metadata,
    )
    return {"status": "saved", "credential": credential}


@router.delete("/credentials/{credential_id}")
async def api_disable_credential(
    credential_id: str,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    ok = await disable_agent_credential(tenant_id=_tenant_id(context), credential_id=credential_id, user_id=_user_id(context))
    if not ok:
        raise HTTPException(status_code=404, detail="credential_not_found")
    return {"status": "disabled", "credential_id": credential_id}


@router.post("/autofill-token")
async def api_issue_autofill_token(
    body: AutofillTokenIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        token = await issue_autofill_token(
            tenant_id=_tenant_id(context),
            credential_id=body.credential_id,
            work_key=body.work_key,
            origin=body.origin,
            user_id=_user_id(context),
            ttl_seconds=body.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return token


@router.post("/autofill-redeem")
async def api_redeem_autofill_token(
    body: AutofillRedeemIn,
    context: TenantContext = Depends(require_member),
) -> dict[str, Any]:
    try:
        return await redeem_autofill_token(
            token=body.token,
            origin=body.origin,
            work_key=body.work_key,
            user_id=_user_id(context),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/access-logs")
async def api_access_logs(
    limit: int = Query(default=50, ge=1, le=200),
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    logs = await list_access_logs(tenant_id=_tenant_id(context), limit=limit)
    return {"logs": logs, "count": len(logs)}


@router.post("/check-action")
async def api_check_action(
    body: CheckActionIn,
    context: TenantContext = Depends(require_viewer),
) -> dict[str, Any]:
    return classify_browser_action(body.action_type, body.summary, body.payload).to_dict()
