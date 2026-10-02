"""AADS-facing adapter around the reusable Browser Bridge service."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Optional

from .pc_agent_budget import begin_call_budget, end_call_budget
from .service import get_browser_bridge_service, headless_work_context_ttl_seconds

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


def server_lane_scope_key(browser_work_key: str | None = None) -> str:
    """서버 레인 컨텍스트 격리 키: work_key > chat:<채팅 세션> > chat:no-session."""
    if browser_work_key:
        return browser_work_key
    try:
        from app.services.tool_executor import current_chat_session_id

        session_id = str(current_chat_session_id.get("") or "").strip().lower()
    except Exception:
        session_id = ""
    session_id = re.sub(r"[^a-z0-9._:-]", "-", session_id)[:100].strip("-._:")
    return f"chat:{session_id or 'no-session'}"


async def _server_lane_context(service: Any, browser_work_key: str | None = None) -> Any:
    scope_key = server_lane_scope_key(browser_work_key)
    await service._evict_idle_headless_work_contexts(headless_work_context_ttl_seconds())
    context = await service._headless_work_context(scope_key)
    service._mark_headless_work_context_used(scope_key)
    return context


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


# PC Agent 경로(lease 대기 + 명령 실행 + 재시도) 도구 1회 호출의 총 대기 상한.
# MCP 클라이언트(Codex tool_timeout_sec=120)가 먼저 끊으면 같은 stdio 연결의
# 다른 도구까지 "Transport closed" 가 된다 — 그보다 먼저 우리가 오류를 돌려준다.
PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS = min(
    _float_env("AADS_PC_AGENT_BROWSER_TIMEOUT_SECONDS", 90.0), 110.0
)
PC_AGENT_TIMEOUT_ERROR = "pc_agent_browser_timeout"

_PC_LANES = {"pc", "pc_agent", "local", "local_pc"}


def normalize_browser_lane(browser_lane: str | None) -> str:
    """"pc" 만 PC Agent 레인. 그 외(빈 값 포함)는 기본값 "server"."""
    return "pc" if str(browser_lane or "").strip().lower() in _PC_LANES else "server"


def pc_agent_timeout_message(stage: str = "") -> str:
    return json.dumps(
        {
            "error": PC_AGENT_TIMEOUT_ERROR,
            "limit_seconds": PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS,
            "stage": stage,
            "hint": (
                "PC Agent 브라우저가 제한 시간 안에 응답하지 않았습니다. 서버 Playwright 로 "
                "충분한 작업이면 browser_lane 을 생략(서버 기본)하고 다시 호출하세요. "
                "자동 전환은 하지 않았습니다."
            ),
        },
        ensure_ascii=False,
    )


async def run_with_pc_agent_deadline(awaitable: Any, stage: str = "") -> Any:
    """PC Agent 경로 awaitable 을 총 시한 안에 끝내고, 넘으면 오류 JSON 문자열을 반환."""
    # wait_for 가 task 를 만들 때 이 맥락을 복사하므로 budget 은 반드시 그 전에 건다.
    budget, token = begin_call_budget(PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS, stage)
    # 안쪽이 CancelledError 를 삼키고 계속 가는 경우에도 시한 시점에 플래그가 먼저 선다.
    cancel_timer = asyncio.get_running_loop().call_later(
        PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS, budget.cancel
    )
    try:
        return await asyncio.wait_for(awaitable, timeout=PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        budget.cancel()
        logger.warning(
            "pc_agent_browser_timeout stage=%s limit=%.0fs",
            stage,
            PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS,
        )
        return pc_agent_timeout_message(stage)
    finally:
        cancel_timer.cancel()
        end_call_budget(token)


async def _acquire_pc_context(
    service: Any,
    browser_session_id: str | None,
    browser_work_key: str | None,
    url: str,
) -> tuple[Any, Optional[str]]:
    budget = PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget

    async def _acquire() -> tuple[Any, Optional[str]]:
        session_id = browser_session_id
        if browser_work_key and not session_id:
            try:
                session = await service.ensure_work_session(
                    work_key=browser_work_key,
                    url=url or "about:blank",
                    queue_wait_timeout_seconds=min(20.0, budget / 3),
                    command_timeout_seconds=max(10.0, budget / 2),
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
            session_id = session.session_id
        return await service.acquire_playwright_context(session_id=session_id or None)

    try:
        return await asyncio.wait_for(_acquire(), timeout=max(1.0, deadline - loop.time()))
    except asyncio.TimeoutError:
        return None, pc_agent_timeout_message("acquire")


async def acquire_browser_context(
    browser_session_id: str | None = None,
    browser_work_key: str | None = None,
    url: str = "about:blank",
    prefer_headless: bool = False,
    browser_lane: str | None = "server",
) -> tuple[Any, Optional[str]]:
    """브라우저 컨텍스트 확보.

    라우팅 정책: 서버 Playwright(headless)가 기본이다. PC Agent 는
    (a) browser_session_id 명시, (b) browser_lane="pc" 명시일 때만 쓴다.
    browser_work_key 단독은 서버 Playwright 의 work_key 단위 컨텍스트 묶음 키다.
    """
    service = get_browser_bridge_service()
    lane = normalize_browser_lane(browser_lane)
    if browser_session_id:
        return await _acquire_pc_context(service, browser_session_id, None, url)
    if browser_work_key and lane == "pc":
        return await _acquire_pc_context(service, None, browser_work_key, url)
    # A URL-only capture is independent server work.  Do not let an unrelated
    # active LOCAL_AGENT/CDP session become its implicit execution target.
    if browser_work_key or prefer_headless:
        try:
            return (
                await asyncio.wait_for(
                    _server_lane_context(service, browser_work_key),
                    timeout=_HEADLESS_LAUNCH_TIMEOUT_SECONDS,
                ),
                None,
            )
        except asyncio.TimeoutError:
            return None, (
                "[브라우저 도구 사용 불가] server_playwright(headless) 초기화가 "
                f"{_HEADLESS_LAUNCH_TIMEOUT_SECONDS:.0f}s 안에 끝나지 않았습니다. "
                "PC Agent로 조용히 전환하지 않습니다 — 로컬 PC/로그인된 브라우저가 꼭 "
                "필요하면 browser_lane='pc' 를 명시하세요."
            )
        except Exception as exc:
            return None, f"[브라우저 도구 사용 불가] server_playwright(headless) 초기화 실패: {exc}"
    return await service.acquire_playwright_context(session_id=None)


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
