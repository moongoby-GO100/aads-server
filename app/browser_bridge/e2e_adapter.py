"""E2E adapter for reusing Browser Bridge sessions when available."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .models import BrowserEndpointKind
from .security import validate_bridge_endpoint
from .service import get_browser_bridge_service


def build_e2e_config(session_id: str | None = None) -> dict[str, Any]:
    """Return Playwright connection hints with headless fallback semantics.

    Environment overrides are intentionally simple so non-AADS runners can use
    the same interface:
      - AADS_BROWSER_BRIDGE_SESSION_ID
      - AADS_BROWSER_BRIDGE_CDP_URL
      - AADS_BROWSER_BRIDGE_WS_URL
      - AADS_BROWSER_BRIDGE_STORAGE_STATE
    """
    session_id = session_id or os.environ.get("AADS_BROWSER_BRIDGE_SESSION_ID") or None

    cdp_url = os.environ.get("AADS_BROWSER_BRIDGE_CDP_URL", "").strip()
    if cdp_url:
        validate_bridge_endpoint(BrowserEndpointKind.CDP, cdp_url)
        return {
            "mode": "cdp",
            "session_id": session_id,
            "cdp_url": cdp_url,
            "headless_fallback": True,
        }

    ws_url = os.environ.get("AADS_BROWSER_BRIDGE_WS_URL", "").strip()
    if ws_url:
        validate_bridge_endpoint(BrowserEndpointKind.WEBSOCKET, ws_url)
        return {
            "mode": "websocket",
            "session_id": session_id,
            "ws_url": ws_url,
            "headless_fallback": True,
        }

    storage_state = os.environ.get("AADS_BROWSER_BRIDGE_STORAGE_STATE", "").strip()
    if storage_state and Path(storage_state).is_file():
        return {
            "mode": "storage_state",
            "session_id": session_id,
            "storage_state_path": storage_state,
            "headless_fallback": True,
        }

    return get_browser_bridge_service().e2e_config(session_id=session_id)
