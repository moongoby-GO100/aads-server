from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.services import external_chat_gateway as gateway

router = APIRouter(prefix="/external/chat", tags=["external-chat"])

Provider = Literal["newtalk"]
ServiceKey = Literal["v1_old", "v1_new", "v2"]


class ExternalSessionRequest(BaseModel):
    provider: Provider = "newtalk"
    service: ServiceKey
    external_user_id: str = Field(..., min_length=1, max_length=200)
    display_name: str = Field(default="", max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExternalMessageRequest(BaseModel):
    content: str = Field(..., min_length=1)
    model_override: str | None = Field(default=None, max_length=100)
    response_mode: str = Field(default="quality", max_length=20)
    metadata: dict[str, Any] = Field(default_factory=dict)


async def require_external_chat_auth(request: Request) -> None:
    settings = gateway.get_settings()
    if not settings.enabled or not (settings.tokens or settings.hmac_secret):
        raise HTTPException(status_code=503, detail="external_chat_not_configured")
    if settings.kill_switch:
        raise HTTPException(status_code=503, detail="external_chat_disabled")

    token = request.headers.get("x-aads-external-token", "")
    auth = request.headers.get("authorization", "")
    if gateway.verify_service_token(token, settings) or gateway.verify_service_token(auth, settings):
        return

    signature = request.headers.get("x-aads-signature", "")
    timestamp = request.headers.get("x-aads-timestamp", "")
    body = await request.body()
    if gateway.verify_hmac_signature(
        body=body,
        signature=signature,
        timestamp=timestamp,
        settings=settings,
    ):
        return

    raise HTTPException(status_code=401, detail="invalid_external_chat_credentials")


@router.get("/config")
async def get_external_chat_config(
    provider: Provider = Query("newtalk"),
    service: ServiceKey = Query(...),
    _: None = Depends(require_external_chat_auth),
):
    return gateway.widget_config(provider, service)


@router.post("/sessions", status_code=201)
async def create_external_chat_session(
    req: ExternalSessionRequest,
    _: None = Depends(require_external_chat_auth),
):
    try:
        return await gateway.create_or_resume_session(
            provider=req.provider,
            service=req.service,
            external_user_id=req.external_user_id,
            display_name=req.display_name,
            metadata=req.metadata,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 400 if detail.endswith("_required") else 503
        raise HTTPException(status_code=status, detail=detail) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/sessions/{external_session_id}/messages")
async def list_external_chat_messages(
    external_session_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    _: None = Depends(require_external_chat_auth),
):
    try:
        return {
            "external_session_id": str(external_session_id),
            "messages": await gateway.list_messages(str(external_session_id), limit=limit),
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/sessions/{external_session_id}/messages")
async def send_external_chat_message(
    external_session_id: UUID,
    req: ExternalMessageRequest,
    _: None = Depends(require_external_chat_auth),
):
    try:
        return await gateway.send_message(
            external_session_id=str(external_session_id),
            content=req.content,
            metadata=req.metadata,
            model_override=req.model_override,
            response_mode=req.response_mode,
        )
    except ValueError as exc:
        detail = str(exc)
        status = 400 if detail.endswith("_required") else 404
        raise HTTPException(status_code=status, detail=detail) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
