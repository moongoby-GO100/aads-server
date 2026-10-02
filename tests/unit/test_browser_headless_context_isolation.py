"""서버 Playwright 레인 컨텍스트 격리: work_key / 채팅 세션 단위 + 유휴 TTL 정리."""
from __future__ import annotations

import pytest

from app.browser_bridge import aads_adapter
from app.browser_bridge.service import BrowserBridgeService
from app.services.tool_executor import current_chat_session_id


class _FakeContext:
    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = []

    def is_connected(self) -> bool:
        return True

    async def new_context(self, **_kw) -> _FakeContext:
        ctx = _FakeContext()
        self.contexts.append(ctx)
        return ctx


def _service() -> BrowserBridgeService:
    svc = BrowserBridgeService()
    browser = _FakeBrowser()
    svc._headless_browser = browser
    svc._headless_context = _FakeContext()
    return svc


@pytest.fixture(autouse=True)
def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AADS_BROWSER_HEADLESS_WORK_CONTEXT_TTL_SECONDS", raising=False)


def _use(monkeypatch: pytest.MonkeyPatch, svc: BrowserBridgeService) -> None:
    monkeypatch.setattr(aads_adapter, "get_browser_bridge_service", lambda: svc)


@pytest.mark.asyncio
async def test_different_work_keys_do_not_share_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()
    _use(monkeypatch, svc)
    ctx_a, err_a = await aads_adapter.acquire_browser_context(browser_work_key="work-a")
    ctx_b, err_b = await aads_adapter.acquire_browser_context(browser_work_key="work-b")
    assert err_a is None and err_b is None
    ctx_a.cookies["session"] = "admin-login"
    assert ctx_a is not ctx_b
    assert ctx_b.cookies == {}
    assert ctx_a is not svc._headless_context and ctx_b is not svc._headless_context


@pytest.mark.asyncio
async def test_same_work_key_reuses_context(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()
    _use(monkeypatch, svc)
    first, _ = await aads_adapter.acquire_browser_context(browser_work_key="work-a")
    second, _ = await aads_adapter.acquire_browser_context(browser_work_key="work-a")
    assert first is second


@pytest.mark.asyncio
async def test_without_work_key_isolated_per_chat_session(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()
    _use(monkeypatch, svc)
    seen = {}
    for sid in ("sess-1", "sess-2", "sess-1", ""):
        token = current_chat_session_id.set(sid)
        try:
            ctx, err = await aads_adapter.acquire_browser_context(prefer_headless=True)
        finally:
            current_chat_session_id.reset(token)
        assert err is None
        assert ctx is not svc._headless_context
        seen.setdefault(sid, []).append(ctx)
    assert seen["sess-1"][0] is seen["sess-1"][1]
    assert seen["sess-1"][0] is not seen["sess-2"][0]
    assert seen[""][0] is not seen["sess-1"][0]
    assert "chat:sess-1" in svc._headless_work_contexts
    assert "chat:no-session" in svc._headless_work_contexts


def test_scope_key_rules() -> None:
    assert aads_adapter.server_lane_scope_key("work-a") == "work-a"
    token = current_chat_session_id.set("ABC/123")
    try:
        assert aads_adapter.server_lane_scope_key(None) == "chat:abc-123"
    finally:
        current_chat_session_id.reset(token)
    assert aads_adapter.server_lane_scope_key(None) == "chat:no-session"


@pytest.mark.asyncio
async def test_idle_contexts_are_evicted_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()
    clock = [1000.0]
    monkeypatch.setattr("app.browser_bridge.service.time.monotonic", lambda: clock[0])
    old = await aads_adapter._server_lane_context(svc, "work-old")
    clock[0] += 1000
    fresh = await aads_adapter._server_lane_context(svc, "work-fresh")
    clock[0] += 1000  # work-old idle 2000s > 1800s, work-fresh idle 1000s
    assert await svc._evict_idle_headless_work_contexts(1800) == 1
    assert old.closed is True and fresh.closed is False
    assert "work-old" not in svc._headless_work_contexts
    assert "work-fresh" in svc._headless_work_contexts
    again = await aads_adapter._server_lane_context(svc, "work-old")
    assert again is not old


@pytest.mark.asyncio
async def test_eviction_runs_on_server_lane_entry_and_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _service()
    clock = [0.0]
    monkeypatch.setattr("app.browser_bridge.service.time.monotonic", lambda: clock[0])
    stale = await aads_adapter._server_lane_context(svc, "work-stale")
    clock[0] += 5000
    monkeypatch.setenv("AADS_BROWSER_HEADLESS_WORK_CONTEXT_TTL_SECONDS", "0")
    await aads_adapter._server_lane_context(svc, "work-other")
    assert stale.closed is False
    monkeypatch.setenv("AADS_BROWSER_HEADLESS_WORK_CONTEXT_TTL_SECONDS", "1800")
    await aads_adapter._server_lane_context(svc, "work-other")
    assert stale.closed is True
    assert "work-stale" not in svc._headless_work_contexts
