"""E2E adapter for reusing Browser Bridge sessions when available."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .models import BrowserEndpointKind
from .security import validate_bridge_endpoint
from .service import get_browser_bridge_service


class PcAgentUnavailable(RuntimeError):
    """PC Agent 가 필요한데 쓸 수 없을 때. 헤드리스로 내려가지 않고 멈춘다.

    브라우저 브릿지는 PC Agent 가 없으면 headless 로 폴백한다. 그런데 헤드리스는
    봇 차단을 통과하지 못한다 — GenSpark 는 헤드리스에 로그인 화면조차 주지 않고
    검증 페이지만 준다(2026-09-13 실측). 즉 PC 가 꺼지면 자동화가 멈추는 게
    아니라 **쓸모없는 결과를 계속 만든다.**

    2026-04 GenSpark 수집기가 정확히 그렇게 고장났다. 세션이 끊긴 뒤 로그인
    화면을 2,406건(그 달 적재의 99%) "대화"로 저장했고, 숫자는 쌓이니 겉보기에는
    정상이었다.

    그래서 PC Agent 가 필요한 작업은 조용히 강등되는 대신 여기서 끊는다.
    """


def build_e2e_config(
    session_id: str | None = None,
    *,
    require_pc_agent: bool = False,
) -> dict[str, Any]:
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

    config = get_browser_bridge_service().e2e_config(session_id=session_id)
    if require_pc_agent:
        _assert_pc_agent(config)
    return config


def _assert_pc_agent(config: dict[str, Any]) -> None:
    """헤드리스·불가 상태면 실행하지 않고 끊는다."""
    mode = str((config or {}).get("mode") or "")
    if mode in {"headless", "unavailable", ""}:
        detail = (config or {}).get("error") or (config or {}).get("fallback_reason") or ""
        raise PcAgentUnavailable(
            f"PC Agent 필요 작업인데 사용할 수 없다 (mode={mode or 'none'}"
            + (f", {detail}" if detail else "")
            + "). 헤드리스로 내려가면 봇 차단을 통과하지 못해 로그인 화면만 수집된다."
        )
