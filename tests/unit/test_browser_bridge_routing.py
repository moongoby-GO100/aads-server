"""AADS-BROWSER-SERVER-PATH-401-P1

일반 사이트 캡처/조작은 PC Agent가 이미 전역 active 세션이어도
서버 Playwright(headless)를 1순위로 써야 한다. browser_session_id나
browser_work_key를 명시했을 때만 PC Agent(또는 다른 Browser Bridge
세션)로 간다. 서버 경로 초기화가 막히면 조용히 PC Agent로 넘어가지
않고 빠르게 실패해야 한다(210초 전체를 태우지 않는다).
"""
from __future__ import annotations

import asyncio

import pytest

from app.browser_bridge import aads_adapter


class _FakeHeadlessContext:
    pass


class _FakeSessionContext:
    pass


class _FakeService:
    """acquire_browser_context가 실제로 건드리는 메서드만 흉내낸다."""

    def __init__(self) -> None:
        self.headless_calls = 0
        self.playwright_context_calls: list[str | None] = []
        self.ensure_work_session_calls: list[str] = []
        self._headless_delay = 0.0

    async def _headless_fallback_context(self):
        self.headless_calls += 1
        if self._headless_delay:
            await asyncio.sleep(self._headless_delay)
        return _FakeHeadlessContext()

    async def acquire_playwright_context(self, session_id: str | None = None):
        # 실제 서비스에서는 session_id=None이면 전역 active 세션(흔히 PC
        # Agent)을 돌려준다. 이 경로가 호출됐다는 것 자체가 "active 세션에
        # 딸려갔다"는 뜻이므로 테스트에서는 호출 여부만 확인하면 충분하다.
        self.playwright_context_calls.append(session_id)
        return _FakeSessionContext(), None

    async def ensure_work_session(self, *, work_key: str, url: str = "about:blank"):
        self.ensure_work_session_calls.append(work_key)
        raise AssertionError("ensure_work_session은 work_key가 명시된 경우에만 호출돼야 한다")


@pytest.fixture()
def fake_service(monkeypatch):
    service = _FakeService()
    monkeypatch.setattr(aads_adapter, "get_browser_bridge_service", lambda: service)
    return service


@pytest.mark.asyncio
async def test_no_session_or_work_key_prefers_server_headless_even_with_active_pc_agent(
    fake_service,
) -> None:
    """browser_session_id/browser_work_key 없이 호출하면, PC Agent가 이미
    전역 active 세션이어도 서버 headless를 쓴다 — acquire_playwright_context
    (active 세션 경로)는 전혀 호출되지 않아야 한다."""
    ctx, err = await aads_adapter.acquire_browser_context(prefer_headless=True)

    assert err is None
    assert isinstance(ctx, _FakeHeadlessContext)
    assert fake_service.headless_calls == 1
    assert fake_service.playwright_context_calls == []


@pytest.mark.asyncio
async def test_explicit_session_id_still_routes_to_pinned_session(fake_service) -> None:
    """browser_session_id를 명시하면 (PC Agent 세션이라도) 그대로 그 세션을
    쓴다 — 명시적 지정은 정책 위반이 아니다."""
    ctx, err = await aads_adapter.acquire_browser_context(
        browser_session_id="sess-pc-agent-1",
        prefer_headless=False,
    )

    assert err is None
    assert isinstance(ctx, _FakeSessionContext)
    assert fake_service.playwright_context_calls == ["sess-pc-agent-1"]
    assert fake_service.headless_calls == 0


@pytest.mark.asyncio
async def test_headless_launch_timeout_fails_fast_without_silent_pc_agent_fallback(
    fake_service, monkeypatch
) -> None:
    """서버 headless 초기화가 멈추면(예: 브라우저 바이너리 문제), 바깥쪽
    210초 도구 타임아웃을 통째로 태우지 않고 짧게 실패해야 하고, 에러 없이
    PC Agent로 조용히 넘어가서도 안 된다."""
    monkeypatch.setattr(aads_adapter, "_HEADLESS_LAUNCH_TIMEOUT_SECONDS", 0.05)
    fake_service._headless_delay = 5.0

    loop = asyncio.get_event_loop()
    start = loop.time()
    ctx, err = await aads_adapter.acquire_browser_context(prefer_headless=True)
    elapsed = loop.time() - start

    assert ctx is None
    assert err is not None
    assert "server_playwright" in err or "headless" in err
    assert elapsed < 2.0, "헤드리스 초기화 실패가 바깥쪽 210초 타임아웃까지 안 늘어져야 한다"
    # 실패했다고 PC Agent(active 세션) 경로로 조용히 넘어가지 않았는지 확인.
    assert fake_service.playwright_context_calls == []


@pytest.mark.asyncio
async def test_ceo_chat_tools_acquire_pw_context_defaults_to_server_first(monkeypatch) -> None:
    """ceo_chat_tools._acquire_pw_context의 기본값이 headless-first인지
    직접 확인한다 — browser_navigate/click/fill 등 모든 인터랙티브 도구가
    이 헬퍼 하나를 공유하므로, 기본값 하나가 전체 라우팅 정책을 결정한다."""
    from app.api import ceo_chat_tools

    captured: dict[str, object] = {}

    async def fake_acquire_browser_context(**kwargs):
        captured.update(kwargs)
        return _FakeHeadlessContext(), None

    # _acquire_pw_context는 호출마다 aads_adapter.acquire_browser_context를
    # 지역 임포트하므로, 모듈 속성을 바꿔치기하면 다음 호출에 바로 반영된다.
    monkeypatch.setattr(
        aads_adapter, "acquire_browser_context", fake_acquire_browser_context
    )

    await ceo_chat_tools._acquire_pw_context()

    assert captured.get("prefer_headless") is True
