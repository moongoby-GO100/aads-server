"""AADS-facing adapter around the reusable Browser Bridge service."""
from __future__ import annotations

import logging
from typing import Any, Optional

from .service import get_browser_bridge_service

logger = logging.getLogger(__name__)


async def acquire_browser_context(
    browser_session_id: str | None = None,
    browser_work_key: str | None = None,
    url: str = "about:blank",
) -> tuple[Any, Optional[str]]:
    service = get_browser_bridge_service()
    if browser_work_key and not browser_session_id:
        try:
            session = await service.ensure_work_session(
                work_key=browser_work_key,
                url=url or "about:blank",
            )
        except Exception as exc:
            logger.warning(
                "browser_work_session_unavailable_headless_fallback "
                "work_key=%s error=%s",
                browser_work_key,
                exc,
            )
            # An unavailable LOCAL_AGENT must not make browser-only work fail
            # outright. Use a fresh server-side headless context explicitly;
            # session_id=None could otherwise select a stale active session.
            try:
                return await service._headless_fallback_context(), None
            except Exception as fallback_exc:
                return None, (
                    f"[브라우저 업무 세션 확보 실패] {exc}; "
                    f"headless fallback 실패: {fallback_exc}"
                )
        browser_session_id = session.session_id
    return await service.acquire_playwright_context(session_id=browser_session_id or None)


def create_pairing_instructions(label: str = "CEO local Chrome", created_by: str = "") -> str:
    service = get_browser_bridge_service()
    pairing = service.create_pairing(label=label, created_by=created_by)
    register_endpoint = "/api/v1/browser-bridge/sessions/register"
    return (
        "[Browser Bridge 페어링]\n"
        f"pairing_id: {pairing.pairing_id}\n"
        f"expires_at: {pairing.expires_at.isoformat()}\n"
        "\n"
        "CEO 로컬 Chrome 또는 브릿지 에이전트에서 아래 one-time token으로 세션을 등록하세요.\n"
        f"registration_endpoint: {register_endpoint}\n"
        f"pairing_token: {pairing.token}\n"
        "\n"
        "CDP 모드는 Chrome을 127.0.0.1에만 바인딩해야 합니다.\n"
        "예: endpoint.kind=cdp, endpoint.url=http://127.0.0.1:9222"
    )
