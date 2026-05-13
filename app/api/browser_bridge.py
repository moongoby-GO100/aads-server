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
    work_key: str = Field(default="", max_length=120)
    protected: bool = False


class SessionSelectRequest(BaseModel):
    session_id: str


class EnsurePcCdpSessionRequest(BaseModel):
    agent_id: str = Field(default="", max_length=120)
    label: str = Field(default="PC Agent Chrome", max_length=120)
    url: str = Field(default="about:blank", max_length=2048)
    preferred_port: int | None = Field(default=None, ge=1024, le=65535)
    isolated_profile: bool = True
    isolation_id: str = Field(default="", max_length=80)
    activate: bool = False
    work_key: str = Field(default="", max_length=120)


class EnsureWorkSessionRequest(BaseModel):
    work_key: str = Field(min_length=2, max_length=120)
    label: str = Field(default="", max_length=120)
    agent_id: str = Field(default="", max_length=120)
    url: str = Field(default="about:blank", max_length=2048)
    preferred_port: int | None = Field(default=None, ge=1024, le=65535)


class WorkSessionRouteExecuteRequest(BaseModel):
    work_key: str = Field(min_length=2, max_length=120)
    command_type: str = Field(min_length=1, max_length=120)
    params: dict[str, Any] = Field(default_factory=dict)
    label: str = Field(default="", max_length=120)
    agent_id: str = Field(default="", max_length=120)
    job_type: str = Field(default="browser_bridge_work_session", max_length=120)
    required_capabilities: list[str] = Field(default_factory=lambda: ["interactive_browser"])
    queue_wait_timeout_seconds: float = Field(default=60, ge=1, le=300)
    lease_ttl_seconds: int = Field(default=120, ge=30, le=3600)
    command_timeout_seconds: float = Field(default=90, ge=1, le=300)
    url: str = Field(default="about:blank", max_length=2048)
    preferred_port: int | None = Field(default=None, ge=1024, le=65535)


class SessionLeaseRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=160)
    preferred_session_id: str = Field(default="", max_length=80)
    ttl_seconds: int = Field(default=300, ge=30, le=3600)


class SessionLeaseReleaseRequest(BaseModel):
    owner: str = Field(default="", max_length=160)
    session_id: str = Field(default="", max_length=80)


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
            work_key=req.work_key,
            protected=req.protected,
        )
    except BrowserBridgeSecurityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "registered", "session": session.public_dict()}


@router.get("/sessions")
async def list_sessions(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    service = get_browser_bridge_service()
    status = service.work_session_status()
    return {"sessions": status["sessions"], "work_sessions": status["work_sessions"]}


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


@router.post("/sessions/ensure-pc-cdp")
async def ensure_pc_cdp_session(
    req: EnsurePcCdpSessionRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    service = get_browser_bridge_service()
    try:
        session = await service.ensure_pc_agent_cdp_session(
            agent_id=req.agent_id,
            label=req.label,
            url=req.url,
            preferred_port=req.preferred_port,
            isolated_profile=req.isolated_profile,
            isolation_id=req.isolation_id,
            activate=req.activate,
            work_key=req.work_key,
        )
    except Exception as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    return {"status": "ready", "session": session.public_dict()}


@router.post("/work-sessions/ensure")
async def ensure_work_session(
    req: EnsureWorkSessionRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    service = get_browser_bridge_service()
    try:
        session = await service.ensure_work_session(
            work_key=req.work_key,
            label=req.label,
            agent_id=req.agent_id,
            url=req.url,
            preferred_port=req.preferred_port,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    return {"status": "ready", "session": session.public_dict()}


@router.post("/work-sessions/route-execute")
async def route_execute_work_session(
    req: WorkSessionRouteExecuteRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    service = get_browser_bridge_service()
    try:
        session = await service.ensure_work_session(
            work_key=req.work_key,
            label=req.label,
            agent_id=req.agent_id,
            url=req.url,
            preferred_port=req.preferred_port,
        )
        endpoint = session.endpoint.public_dict()
        metadata = endpoint.get("metadata", {}) if isinstance(endpoint, dict) else {}
        agent_id = str(req.agent_id or metadata.get("agent_id") or "")
        port = metadata.get("port")
        params = dict(req.params or {})
        params.setdefault("work_key", session.work_key or req.work_key)
        params.setdefault("browser_session_id", session.session_id)
        params.setdefault("session_id", session.session_id)
        params.setdefault("label", session.label or req.label)
        if port:
            params.setdefault("port", int(port))
            params.setdefault("preferred_port", int(port))

        result = await service._execute_pc_agent_route_via_active_api(
            command_type=req.command_type,
            params=params,
            agent_id=agent_id,
            job_type=req.job_type,
            required_capabilities=req.required_capabilities,
            queue_wait_timeout_seconds=req.queue_wait_timeout_seconds,
            lease_ttl_seconds=req.lease_ttl_seconds,
            command_timeout_seconds=req.command_timeout_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=503, detail="No active PC Agent route API is available")
    return result


@router.get("/work-sessions")
async def list_work_sessions(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    service = get_browser_bridge_service()
    return service.work_session_status()


@router.post("/sessions/lease")
async def lease_session(
    req: SessionLeaseRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    service = get_browser_bridge_service()
    try:
        session = service.sessions.acquire_lease(
            owner=req.owner,
            preferred_session_id=req.preferred_session_id,
            ttl_seconds=req.ttl_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "leased", "session": session.public_dict()}


@router.post("/sessions/release-lease")
async def release_session_lease(
    req: SessionLeaseReleaseRequest,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    service = get_browser_bridge_service()
    released = service.sessions.release_lease(owner=req.owner, session_id=req.session_id)
    return {"status": "released", "released": released}


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
