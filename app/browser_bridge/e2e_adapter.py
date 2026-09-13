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

    # 환경변수가 비어 있으면 기본 경로를 본다.
    #
    # 시각 QA 브라우저가 인증 없이 페이지를 열어, /, /chat, /ops 세 장이 전부
    # 로그인 화면이었다(2026-09-13 실측: 스크린샷 4장이 23,241바이트로 바이트
    # 동일, 감리 요약도 "로그인 UI"라고 적었다). 캡처 스크립트는 storage_state
    # 를 이미 지원하는데 아무도 넘겨주지 않았을 뿐이다.
    #
    # 환경변수로만 받으면 docker-compose 를 고쳐야 하고 그건 재시작을 부른다.
    # browser-bridge-state 는 이미 붙어 있는 쓰기 가능 마운트이므로, 거기
    # 파일이 있으면 쓴다. scripts/refresh_qa_storage_state.py 가 QA 직전에
    # 이 파일을 새로 만든다 — 토큰이 만료되면 조용히 로그인 화면으로
    # 되돌아가므로 크론이 아니라 QA 직전 갱신이어야 한다.
    storage_state = os.environ.get("AADS_BROWSER_BRIDGE_STORAGE_STATE", "").strip()
    if not storage_state:
        _default_state = "/app/browser-bridge-state/qa-storage-state.json"
        if Path(_default_state).is_file():
            storage_state = _default_state
    if storage_state and Path(storage_state).is_file():
        return {
            "mode": "storage_state",
            "session_id": session_id,
            "storage_state_path": storage_state,
            "headless_fallback": True,
        }

    return get_browser_bridge_service().e2e_config(session_id=session_id)
