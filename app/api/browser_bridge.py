"""Browser Bridge API.

This router is intentionally generic: AADS chat tools, local bridge agents, and
E2E runners share the same pairing/session contract.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.browser_bridge.models import BrowserEndpointKind
from app.browser_bridge.security import BrowserBridgeSecurityError
from app.browser_bridge.service import get_browser_bridge_service

router = APIRouter(prefix="/browser-bridge", tags=["browser-bridge"])


class PairingCreateRequest(BaseModel):
    label: str = Field(default="CEO local Chrome", max_length=120)
    ttl_seconds: int = Field(default=600, ge=60, le=3600)


class EndpointRegistration(BaseModel):
    kind: Literal["cdp", "websocket", "local_agent", "storage_state"]
    url: str | None = Field(default=None, max_length=2048)
    browser_name: str = Field(default="chromium", max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionRegisterRequest(BaseModel):
    pairing_token: str = Field(min_length=16, max_length=256)
    label: str = Field(default="CEO local Chrome", max_length=120)
    endpoint: EndpointRegistration
    storage_state: dict[str, Any] | None = None
    activate: bool = True
    expires_hours: int | None = Field(default=None, ge=1, le=24 * 30)


class SessionSelectRequest(BaseModel):
    session_id: str


@router.post("/pairings")
async def create_pairing(
    req: PairingCreateRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    service = get_browser_bridge_service()
    pairing = service.create_pairing(
        label=req.label,
        created_by=current_user.get("email", ""),
        ttl_seconds=req.ttl_seconds,
    )
    payload = pairing.public_dict()
    payload.update(
        {
            "registration_endpoint": "/api/v1/browser-bridge/sessions/register",
            "local_defaults": {
                "bind_host": "127.0.0.1",
                "allowed_endpoint_kinds": [
                    BrowserEndpointKind.CDP.value,
                    BrowserEndpointKind.WEBSOCKET.value,
                    BrowserEndpointKind.LOCAL_AGENT.value,
                    BrowserEndpointKind.STORAGE_STATE.value,
                ],
            },
        }
    )
    return payload


@router.post("/sessions/register")
async def register_session(req: SessionRegisterRequest) -> dict[str, Any]:
    service = get_browser_bridge_service()
    try:
        session = service.register_session(
            pairing_token=req.pairing_token,
            label=req.label,
            endpoint_kind=req.endpoint.kind,
            endpoint_url=req.endpoint.url,
            browser_name=req.endpoint.browser_name,
            storage_state=req.storage_state,
            metadata=req.endpoint.metadata,
            activate=req.activate,
            expires_hours=req.expires_hours,
        )
    except BrowserBridgeSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "registered", "session": session.public_dict()}


@router.get("/sessions")
async def list_sessions(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    service = get_browser_bridge_service()
    return {"sessions": list(service.sessions.public_sessions())}


@router.post("/sessions/select")
async def select_session(
    req: SessionSelectRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    service = get_browser_bridge_service()
    try:
        session = service.select_session(req.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "selected", "session": session.public_dict()}


@router.get("/e2e/config")
async def e2e_config(
    session_id: str | None = Query(default=None, description="특정 Browser Bridge session id"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    config = get_browser_bridge_service().e2e_config(session_id=session_id)
    return {
        "config": config,
        "env_interface": {
            "AADS_BROWSER_BRIDGE_SESSION_ID": "registered session id",
            "AADS_BROWSER_BRIDGE_CDP_URL": "http://127.0.0.1:9222 or ws://127.0.0.1:9222/devtools/browser/...",
            "AADS_BROWSER_BRIDGE_WS_URL": "ws://127.0.0.1:<port>/<playwright-ws>",
            "AADS_BROWSER_BRIDGE_STORAGE_STATE": "/absolute/path/to/storage_state.json",
        },
        "fallback": "Headless Playwright is used when no bridge session is available.",
    }
