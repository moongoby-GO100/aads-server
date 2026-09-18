"""AADS-facing adapter around the reusable Browser Bridge service."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from .service import get_browser_bridge_service

logger = logging.getLogger(__name__)

# 서버 Playwright(headless) 경로가 막히면(브라우저 바이너리 깨짐 등) 빨리
# 실패해야 한다. 여기서 막히면 바깥쪽 도구 타임아웃(최대 210s)을 통째로
# 태우고서야 에러가 드러난다 — AADS-BROWSER-SERVER-PATH-401-P1.
_HEADLESS_LAUNCH_TIMEOUT_SECONDS = float(
    os.getenv("AADS_BROWSER_HEADLESS_LAUNCH_TIMEOUT_SECONDS", "25")
)


async def _headless_context_with_timeout(service: Any) -> Any:
    return await asyncio.wait_for(
        service._headless_fallback_context(),
        timeout=_HEADLESS_LAUNCH_TIMEOUT_SECONDS,
    )


async def acquire_browser_context(
    browser_session_id: str | None = None,
    browser_work_key: str | None = None,
    url: str = "about:blank",
    prefer_headless: bool = False,
) -> tuple[Any, Optional[str]]:
    service = get_browser_bridge_service()
    # A URL-only capture is independent server work.  Do not let an unrelated
    # active LOCAL_AGENT/CDP session become its implicit execution target.
    if prefer_headless and not browser_session_id and not browser_work_key:
        try:
            return await _headless_context_with_timeout(service), None
        except asyncio.TimeoutError:
            return None, (
                "[브라우저 도구 사용 불가] server_playwright(headless) 초기화가 "
                f"{_HEADLESS_LAUNCH_TIMEOUT_SECONDS:.0f}s 안에 끝나지 않았습니다. "
                "PC Agent로 조용히 전환하지 않습니다 — 필요하면 browser_work_key로 "
                "PC Agent 세션을 명시적으로 지정하세요."
            )
        except Exception as exc:
            return None, f"[브라우저 도구 사용 불가] server_playwright(headless) 초기화 실패: {exc}"
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
                return await _headless_context_with_timeout(service), None
            except asyncio.TimeoutError:
                return None, (
                    f"[브라우저 업무 세션 확보 실패] {exc}; "
                    f"headless fallback도 {_HEADLESS_LAUNCH_TIMEOUT_SECONDS:.0f}s 안에 "
                    "끝나지 않았습니다"
                )
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
