"""browser_clear_site_session: 작업공간(work_key)의 지정 사이트 로그인 기록만 비운다."""
from __future__ import annotations

import json
import logging

import pytest

from app.api import ceo_chat_tools
from app.browser_bridge import service as bridge_service
from app.browser_bridge.service import (
    BrowserBridgeService,
    SiteSessionClearRejected,
    parse_site_session_origin,
)
from app.services import tool_registry
from app.services.tool_executor import ToolExecutor, current_chat_session_id

SECRET = "SECRET-COOKIE-VALUE-123"


def _cookie(domain: str, name: str = "sid", path: str = "/") -> dict:
    return {"name": name, "value": SECRET, "domain": domain, "path": path}


class _FakePage:
    def __init__(self, url: str, ctx: "_FakeContext") -> None:
        self.url = url
        self.ctx = ctx
        self.closed = False
        self.evaluated = 0

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, _script: str) -> None:
        self.evaluated += 1
        self.ctx.storage_cleared_for.append(self.url)

    async def route(self, *_a, **_k) -> None:
        return None

    async def goto(self, url: str, **_k) -> None:
        self.url = url

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = list(cookies)
        self.pages: list[_FakePage] = []
        self.storage_cleared_for: list[str] = []

    async def cookies(self) -> list[dict]:
        return [dict(c) for c in self._cookies]

    async def clear_cookies(self, *, name: str, domain: str, path: str) -> None:
        self._cookies = [
            c for c in self._cookies
            if not (c["name"] == name and c["domain"] == domain and c["path"] == path)
        ]

    async def new_page(self) -> _FakePage:
        page = _FakePage("about:blank", self)
        self.pages.append(page)
        return page


class _FakeBrowser:
    def is_connected(self) -> bool:
        return True


def _service(**contexts: _FakeContext) -> BrowserBridgeService:
    svc = BrowserBridgeService()
    browser = _FakeBrowser()
    for key, ctx in contexts.items():
        svc._headless_work_contexts[key.replace("_", "-")] = (browser, ctx)
    return svc


def _ably_cookies() -> list[dict]:
    return [
        _cookie(".a-bly.com", "parent"),
        _cookie("my.a-bly.com", "host"),
        _cookie("other.a-bly.com", "sibling"),
        _cookie(".google.com", "ga"),
        _cookie("example.com", "other"),
    ]


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    def _apply(svc: BrowserBridgeService) -> BrowserBridgeService:
        monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: svc)
        return svc

    return _apply


@pytest.mark.asyncio
async def test_clears_only_target_site_cookies(patched) -> None:
    ctx = _FakeContext(_ably_cookies())
    svc = _service(ably_admin=ctx)
    result = await svc.clear_headless_work_site_session("ably-admin", "https://my.a-bly.com")

    assert result["cleared_cookie_count"] == 2
    assert result["cookie_domains"] == [".a-bly.com", "my.a-bly.com"]
    left = {c["name"] for c in ctx._cookies}
    assert left == {"sibling", "ga", "other"}


@pytest.mark.asyncio
async def test_other_work_key_context_untouched() -> None:
    target = _FakeContext(_ably_cookies())
    other = _FakeContext(_ably_cookies())
    svc = _service(ably_admin=target, linda_shop=other)
    await svc.clear_headless_work_site_session("ably-admin", "https://my.a-bly.com")

    assert len(other._cookies) == 5
    assert other.storage_cleared_for == []
    assert len(target._cookies) == 3


@pytest.mark.asyncio
async def test_storage_cleared_on_open_page_of_origin_only() -> None:
    ctx = _FakeContext(_ably_cookies())
    on_site = _FakePage("https://my.a-bly.com/dashboard", ctx)
    off_site = _FakePage("https://example.com/", ctx)
    ctx.pages = [on_site, off_site]
    svc = _service(ably_admin=ctx)
    result = await svc.clear_headless_work_site_session("ably-admin", "https://my.a-bly.com/")

    assert result["storage_cleared"] is True
    assert on_site.evaluated == 1
    assert off_site.evaluated == 0


@pytest.mark.asyncio
async def test_storage_cleared_via_blank_page_when_no_open_page() -> None:
    ctx = _FakeContext([])
    svc = _service(ably_admin=ctx)
    result = await svc.clear_headless_work_site_session("ably-admin", "https://my.a-bly.com")

    assert result["storage_cleared"] is True
    assert ctx.storage_cleared_for == ["https://my.a-bly.com/"]
    assert all(p.closed for p in ctx.pages)


@pytest.mark.asyncio
async def test_missing_context_is_not_created() -> None:
    svc = BrowserBridgeService()
    result = await svc.clear_headless_work_site_session("ably-admin", "https://my.a-bly.com")

    assert result["context_present"] is False
    assert result["cleared_cookie_count"] == 0
    assert svc._headless_work_contexts == {}


@pytest.mark.parametrize(
    ("origin", "code"),
    [
        ("", "origin_required"),
        ("https://*.a-bly.com", "wildcard_not_allowed"),
        ("https://aads.newtalk.kr", "internal_domain"),
        ("https://newtalk.kr", "internal_domain"),
        ("https://localhost", "internal_domain"),
        ("https://127.0.0.1", "internal_domain"),
        ("http://my.a-bly.com", "origin_invalid"),
        ("https://my.a-bly.com/admin", "origin_invalid"),
        ("https://co.kr", "origin_invalid"),
    ],
)
def test_origin_rejections(origin: str, code: str) -> None:
    with pytest.raises(SiteSessionClearRejected) as exc:
        parse_site_session_origin(origin)
    assert exc.value.code == code


def test_cookie_domain_scope() -> None:
    assert parse_site_session_origin("https://my.a-bly.com")[2] == ("my.a-bly.com", "a-bly.com")
    assert parse_site_session_origin("https://a-bly.com")[2] == ("a-bly.com",)
    assert parse_site_session_origin("https://shop.example.co.kr")[2] == ("shop.example.co.kr", "example.co.kr")


@pytest.mark.asyncio
async def test_tool_rejects_missing_work_key_and_origin(patched) -> None:
    patched(_service(ably_admin=_FakeContext(_ably_cookies())))
    no_key = await ceo_chat_tools.tool_browser_clear_site_session("", "https://my.a-bly.com")
    no_origin = await ceo_chat_tools.tool_browser_clear_site_session("ably-admin", "")
    assert "work_key_required" in no_key
    assert "origin_required" in no_origin


@pytest.mark.asyncio
async def test_tool_rejects_wildcard_and_internal(patched) -> None:
    ctx = _FakeContext(_ably_cookies())
    patched(_service(ably_admin=ctx))
    wildcard = await ceo_chat_tools.tool_browser_clear_site_session("ably-admin", "https://*.a-bly.com")
    internal = await ceo_chat_tools.tool_browser_clear_site_session("ably-admin", "https://aads.newtalk.kr")
    assert "wildcard_not_allowed" in wildcard
    assert "internal_domain" in internal
    assert len(ctx._cookies) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"browser_lane": "pc"}, {"browser_session_id": "sess-1"}])
async def test_tool_rejects_pc_lane(patched, kwargs) -> None:
    ctx = _FakeContext(_ably_cookies())
    patched(_service(ably_admin=ctx))
    out = await ceo_chat_tools.tool_browser_clear_site_session(
        "ably-admin", "https://my.a-bly.com", **kwargs
    )
    assert "pc_lane_unsupported" in out
    assert len(ctx._cookies) == 5


@pytest.mark.asyncio
async def test_tool_result_and_audit_carry_no_cookie_values(patched, caplog) -> None:
    patched(_service(ably_admin=_FakeContext(_ably_cookies())))
    token = current_chat_session_id.set("65f926cb-chat")
    try:
        with caplog.at_level(logging.INFO):
            out = await ceo_chat_tools.tool_browser_clear_site_session("ably-admin", "https://my.a-bly.com")
    finally:
        current_chat_session_id.reset(token)

    data = json.loads(out)
    assert data["cleared_cookie_count"] == 2
    assert SECRET not in out
    assert "value" not in out
    audit = [r.getMessage() for r in caplog.records if "browser_clear_site_session_audit" in r.getMessage()]
    assert len(audit) == 1
    assert "work_key=ably-admin" in audit[0]
    assert "origin=https://my.a-bly.com" in audit[0]
    assert "cleared=2" in audit[0]
    assert "chat_session=65f926cb-chat" in audit[0]
    assert SECRET not in audit[0]


@pytest.mark.asyncio
async def test_executor_dispatch_routes_to_tool(patched) -> None:
    ctx = _FakeContext(_ably_cookies())
    patched(_service(ably_admin=ctx))
    out = await ToolExecutor().execute(
        "browser_clear_site_session",
        {"browser_work_key": "ably-admin", "origin": "https://my.a-bly.com"},
    )
    assert "cleared_cookie_count" in str(out)
    assert len(ctx._cookies) == 3


def test_tool_registered_everywhere() -> None:
    from app.services import tool_executor

    names = {t["name"] for t in ceo_chat_tools.TOOL_DEFINITIONS}
    assert "browser_clear_site_session" in names
    assert "browser_clear_site_session" in tool_registry._TOOLS
    assert "browser_clear_site_session" in tool_executor._BROWSER_TOOLS
    assert "browser_clear_site_session" in tool_executor._INTENT_TOOL_MAP["browser"]
    assert "browser_clear_site_session" in tool_registry.INTENT_REQUIRED_TOOLS["browser"]
    schema = tool_registry._TOOLS["browser_clear_site_session"]["input_schema"]
    assert schema["required"] == ["browser_work_key", "origin"]
