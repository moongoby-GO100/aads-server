"""AADS-BROWSER-SERVER-PATH-401-P1

일반 사이트 캡처/조작은 PC Agent가 이미 전역 active 세션이어도
서버 Playwright(headless)를 1순위로 써야 한다. browser_session_id나
browser_work_key를 명시했을 때만 PC Agent(또는 다른 Browser Bridge
세션)로 간다. 서버 경로 초기화가 막히면 조용히 PC Agent로 넘어가지
않고 빠르게 실패해야 한다(210초 전체를 태우지 않는다).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

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
        self.work_context_calls: list[str] = []
        self._work_contexts: dict[str, _FakeHeadlessContext] = {}
        self._headless_delay = 0.0
        self._ensure_delay = 0.0

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

    async def _headless_work_context(self, work_key: str):
        self.work_context_calls.append(work_key)
        if self._headless_delay:
            await asyncio.sleep(self._headless_delay)
        return self._work_contexts.setdefault(work_key, _FakeHeadlessContext())

    async def _evict_idle_headless_work_contexts(self, ttl=None):
        return 0

    def _mark_headless_work_context_used(self, work_key: str) -> None:
        return None

    async def ensure_work_session(self, *, work_key: str, url: str = "about:blank", **_kwargs):
        self.ensure_work_session_calls.append(work_key)
        if self._ensure_delay:
            await asyncio.sleep(self._ensure_delay)
        return SimpleNamespace(session_id=f"bb-{work_key}")


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
    # work_key 없는 서버 레인은 공용 fallback 컨텍스트가 아니라 채팅 세션 단위 컨텍스트.
    assert fake_service.headless_calls == 0
    assert fake_service.work_context_calls == ["chat:no-session"]
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


# ── AADS-BROWSER-SERVER-PW-FIRST-20261002 ─────────────────────────────────


@pytest.mark.asyncio
async def test_work_key_only_uses_server_playwright_not_pc_agent(fake_service) -> None:
    """browser_work_key 만 준 호출은 서버 Playwright — PC Agent ensure_work_session 미호출."""
    ctx, err = await aads_adapter.acquire_browser_context(
        browser_work_key="go100-ops-test",
        url="https://go100.newtalk.kr/go100/strategies/310/operations",
        prefer_headless=True,
    )

    assert err is None
    assert isinstance(ctx, _FakeHeadlessContext)
    assert fake_service.ensure_work_session_calls == []
    assert fake_service.playwright_context_calls == []
    assert fake_service.work_context_calls == ["go100-ops-test"]


@pytest.mark.asyncio
async def test_same_work_key_reuses_same_server_context(fake_service) -> None:
    first, _ = await aads_adapter.acquire_browser_context(browser_work_key="wk-a", prefer_headless=True)
    again, _ = await aads_adapter.acquire_browser_context(browser_work_key="wk-a", prefer_headless=True)
    other, _ = await aads_adapter.acquire_browser_context(browser_work_key="wk-b", prefer_headless=True)

    assert first is again
    assert other is not first


@pytest.mark.asyncio
async def test_lane_pc_with_work_key_routes_to_pc_agent(fake_service) -> None:
    ctx, err = await aads_adapter.acquire_browser_context(
        browser_work_key="wk-pc", browser_lane="pc", prefer_headless=True
    )

    assert err is None
    assert isinstance(ctx, _FakeSessionContext)
    assert fake_service.ensure_work_session_calls == ["wk-pc"]
    assert fake_service.playwright_context_calls == ["bb-wk-pc"]
    assert fake_service.work_context_calls == []


@pytest.mark.asyncio
async def test_session_id_routes_to_pc_even_with_server_lane(fake_service) -> None:
    ctx, err = await aads_adapter.acquire_browser_context(
        browser_session_id="bb-x", browser_work_key="wk", browser_lane="server"
    )

    assert err is None
    assert isinstance(ctx, _FakeSessionContext)
    assert fake_service.playwright_context_calls == ["bb-x"]
    assert fake_service.work_context_calls == []


@pytest.mark.asyncio
async def test_pc_agent_acquire_over_deadline_returns_error_fast(fake_service, monkeypatch) -> None:
    monkeypatch.setattr(aads_adapter, "PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS", 0.05)
    fake_service._ensure_delay = 5.0

    loop = asyncio.get_event_loop()
    start = loop.time()
    ctx, err = await aads_adapter.acquire_browser_context(browser_work_key="wk-slow", browser_lane="pc")
    elapsed = loop.time() - start

    assert ctx is None
    assert json.loads(err)["error"] == "pc_agent_browser_timeout"
    assert elapsed < 2.0
    # 조용한 서버 전환 금지
    assert fake_service.work_context_calls == []
    assert fake_service.headless_calls == 0


def test_pc_agent_default_deadline_is_under_mcp_client_timeout() -> None:
    assert aads_adapter.PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS <= 90.0


@pytest.mark.asyncio
async def test_pc_tool_call_total_wait_is_bounded(monkeypatch) -> None:
    """PC 경로(lane=pc/session_id) 도구 1회 호출은 acquire 이후 명령 실행까지 포함해 시한을 넘기지 않는다."""
    from app.api import ceo_chat_tools

    monkeypatch.setattr(aads_adapter, "PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS", 0.05)

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(ceo_chat_tools, "_acquire_pw_context", hang)

    loop = asyncio.get_event_loop()
    start = loop.time()
    by_lane = await ceo_chat_tools.tool_browser_snapshot(browser_work_key="wk", browser_lane="pc")
    by_session = await ceo_chat_tools.tool_browser_navigate(
        "https://aads.newtalk.kr/", browser_session_id="bb-1"
    )
    elapsed = loop.time() - start

    assert json.loads(by_lane)["error"] == "pc_agent_browser_timeout"
    assert json.loads(by_session)["error"] == "pc_agent_browser_timeout"
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_server_lane_tool_call_is_not_wrapped_by_pc_deadline(monkeypatch) -> None:
    from app.api import ceo_chat_tools

    monkeypatch.setattr(aads_adapter, "PC_AGENT_BROWSER_TOTAL_TIMEOUT_SECONDS", 0.01)
    captured: dict[str, object] = {}

    class _Page:
        async def title(self):
            return "t"

    class _Ctx:
        pages = [_Page()]

    async def slow_acquire(*args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        await asyncio.sleep(0.1)
        return None, "stub-error"

    monkeypatch.setattr(ceo_chat_tools, "_acquire_pw_context", slow_acquire)

    result = await ceo_chat_tools.tool_browser_snapshot(browser_work_key="wk-server")

    assert result == "stub-error"
    assert captured["args"] == ("", "wk-server")
    assert captured["browser_lane"] == ""


@pytest.mark.asyncio
async def test_headless_work_context_is_isolated_per_key_and_reused() -> None:
    from app.browser_bridge.service import BrowserBridgeService

    class _Ctx:
        def __init__(self) -> None:
            self.closed = False

        async def close(self):
            self.closed = True

    class _Browser:
        def __init__(self) -> None:
            self.contexts: list[_Ctx] = []

        def is_connected(self) -> bool:
            return True

        async def new_context(self, **_kwargs):
            ctx = _Ctx()
            self.contexts.append(ctx)
            return ctx

    service = BrowserBridgeService()
    browser = _Browser()
    service._headless_browser = browser
    service._headless_context = _Ctx()

    a1 = await service._headless_work_context("wk-a")
    a2 = await service._headless_work_context("wk-a")
    b = await service._headless_work_context("wk-b")

    assert a1 is a2
    assert b is not a1
    assert a1 is not service._headless_context


def test_browser_lane_schema_in_registry_and_chat_tools() -> None:
    from app.api import ceo_chat_tools
    from app.services.tool_registry import ToolRegistry

    names = [f"browser_{n}" for n in (
        "navigate", "snapshot", "screenshot", "click", "fill", "press_key",
        "select_option", "check", "upload_file", "download", "tab_list",
    )] + ["capture_screenshot"]
    registry = ToolRegistry().get_all_tools()
    chat = {t["name"]: t for t in ceo_chat_tools.TOOL_DEFINITIONS}

    for name in names:
        for props in (
            registry[name]["input_schema"]["properties"],
            chat[name]["input_schema"]["properties"],
        ):
            assert props["browser_lane"]["enum"] == ["server", "pc"], name
            assert "PC Agent 로 가지 않음" in props["browser_work_key"]["description"], name
