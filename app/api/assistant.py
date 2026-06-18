"""
CEO personal assistant control-plane APIs.

This router exposes readiness metadata only. Connector credentials and tokens
must stay behind each connector's OAuth or vault flow.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.auth import TenantRole, require_tenant_role
from app.services.pc_agent_manager import pc_agent_manager

router = APIRouter()


def _require_internal_admin(context: dict[str, Any]) -> None:
    user = context.get("user") or {}
    if not user.get("is_internal_admin"):
        raise HTTPException(status_code=403, detail="Personal Assistant Hub is internal-admin only")


def _status(enabled: bool, *, detail: str, action: str = "") -> dict[str, Any]:
    return {
        "status": "ready" if enabled else "planned",
        "ready": enabled,
        "detail": detail,
        "next_action": action,
    }


@router.get("/assistant/readiness", summary="CEO Personal Assistant readiness")
async def get_assistant_readiness(
    context: dict[str, Any] = Depends(require_tenant_role(TenantRole.VIEWER)),
) -> dict[str, Any]:
    """Return internal-admin-only readiness for the Jarvis-style assistant hub."""
    _require_internal_admin(context)

    agents = pc_agent_manager.list_agent_statuses()
    online_agents = [agent for agent in agents if agent.get("status") == "online"]

    connectors = {
        "pc_agent": {
            "status": "ready" if online_agents else "attention",
            "ready": bool(online_agents),
            "online_count": len(online_agents),
            "detail": "Windows shell, screenshot, and browser routing are available when a CEO PC Agent is online.",
            "next_action": "" if online_agents else "Start or reconnect the CEO PC Agent.",
        },
        "google_calendar": _status(
            False,
            detail="Calendar connector contract is not wired yet.",
            action="Add OAuth credential flow and calendar read scope.",
        ),
        "gmail": _status(
            False,
            detail="Gmail connector contract is not wired yet.",
            action="Add OAuth credential flow and read/send approval policy.",
        ),
        "kakao": _status(
            False,
            detail="Kakao bot and PC Agent commands exist, but personal Kakao readiness is not exposed as a connector.",
            action="Bind Kakao command readiness to PC Agent lease and approval policy.",
        ),
        "files": _status(
            True,
            detail="Chat drive and artifact APIs are available under tenant scope.",
        ),
        "approval_policy": _status(
            True,
            detail="High-risk shell, git, and deploy operations are separated from hard-deny operations.",
        ),
    }

    ready_count = sum(1 for item in connectors.values() if item["ready"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tenant": context.get("tenant"),
        "mode": "personal_assistant",
        "summary": {
            "ready": ready_count,
            "total": len(connectors),
            "attention": len(connectors) - ready_count,
        },
        "connectors": connectors,
    }
