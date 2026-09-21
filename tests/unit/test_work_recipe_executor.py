from __future__ import annotations

import pytest

from app.services.work_recipe import executor as executor_module


class FakeLocator:
    async def aria_snapshot(self):
        return "snapshot"

    async def inner_text(self, **kwargs):
        return "읽기 전용 대시보드"


class FakePage:
    def __init__(self):
        self.calls = []
        self.url = "about:blank"

    async def goto(self, url, **kwargs):
        self.calls.append(("navigate", {"url": url, **kwargs}))
        self.url = url

    async def click(self, selector, **kwargs):
        self.calls.append(("click", {"selector": selector, **kwargs}))

    async def fill(self, selector, value, **kwargs):
        self.calls.append(("fill", {"selector": selector, "value": value, **kwargs}))

    async def select_option(self, selector, value, **kwargs):
        self.calls.append(("select", {"selector": selector, "value": value, **kwargs}))

    async def press(self, selector, key, **kwargs):
        self.calls.append(("press", {"selector": selector, "key": key, **kwargs}))

    async def set_input_files(self, selector, value, **kwargs):
        self.calls.append(("upload", {"selector": selector, "value": value, **kwargs}))

    async def download(self, selector, **kwargs):
        self.calls.append(("download", {"selector": selector, **kwargs}))
        return {"path": "/tmp/download"}

    def locator(self, selector):
        self.calls.append(("snapshot", {"selector": selector}))
        return FakeLocator()

    async def evaluate(self, expression, argument, **kwargs):
        self.calls.append(("api_call", {"argument": argument, **kwargs}))
        return {"status": 200, "ok": True, "text": "ok"}

    async def screenshot(self, **kwargs):
        # 실제 Playwright 는 timeout 을 받는다. 스텁이 그것을 거부하면
        # 증거 수집이 TypeError 로 삼켜져 'unavailable' 이 되고,
        # 테스트는 통과하던 경로가 아니라 실패 경로를 검증하게 된다.
        self.screenshot_kwargs = dict(kwargs)
        return b"smart-browser-screen"


class FakeContext:
    def __init__(self, page):
        self.pages = [page]


@pytest.mark.parametrize(
    ("action", "fields"),
    [
        ("navigate", {"url": "https://example.com"}),
        ("click", {"selector": "#go"}),
        ("fill", {"selector": "#name", "value": "Kim"}),
        ("select", {"selector": "#kind", "value": "one"}),
        ("press", {"selector": "#q", "value": "Enter"}),
        ("upload", {"selector": "#file", "value": "/tmp/a.txt"}),
        ("download", {"selector": "#download", "value": "/tmp"}),
        ("snapshot", {"selector": "main"}),
        ("api_call", {"endpoint": "/api/items", "value": {"method": "POST"}}),
    ],
)
async def test_executor_dispatches_all_allowed_actions(monkeypatch, action, fields):
    page = FakePage()

    async def acquire(**kwargs):
        return FakeContext(page), None

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    executor = executor_module.BrowserRecipeExecutor()
    result = await executor({"action": action, "risk": "READ", **fields})

    assert result["ok"] is True
    executed = next(call for call in page.calls if call[0] == action)
    assert executed[0] == action
    if action == "api_call":
        assert executed[1]["argument"] == {
            "endpoint": "/api/items",
            "options": {"method": "POST"},
        }


async def test_executor_reports_missing_bridge(monkeypatch):
    async def acquire(**kwargs):
        return None, "연결된 브라우저가 없습니다"

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    result = await executor_module.BrowserRecipeExecutor()(
        {"action": "navigate", "url": "https://example.com", "risk": "READ"}
    )
    assert result["ok"] is False
    assert "Browser Bridge" in result["error"]


async def test_executor_prefers_browser_agent_and_returns_visual_dom_aria_evidence(monkeypatch):
    page = FakePage()
    received = {}

    async def acquire(**kwargs):
        received.update(kwargs)
        return FakeContext(page), None

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    result = await executor_module.BrowserRecipeExecutor(browser_work_key="local-pc")(
        {"action": "snapshot", "risk": "READ", "url": "https://aads.newtalk.kr/ops"}
    )

    assert result["ok"] is True
    assert received["prefer_headless"] is True
    assert received["browser_work_key"] is None
    assert result["route"] == "browser_agent"
    assert result["evidence"]["screenshot"]["status"] == "captured"
    assert result["evidence"]["dom"]["text"] == "읽기 전용 대시보드"
    assert result["evidence"]["aria"]["text"] == "snapshot"
    # 캡처가 무한정 매달리면 읽기 한 건이 도구 타임아웃을 넘긴다.
    assert page.screenshot_kwargs.get("timeout") == 15_000


async def test_screenshot_evidence_carries_openable_url(monkeypatch):
    """해시만 남기면 "증거 저장" 이라고 보고해도 아무도 그 화면을 못 연다."""
    page = FakePage()

    async def acquire(**kwargs):
        return FakeContext(page), None

    async def fake_save(data, *, prefix=""):
        assert data == b"smart-browser-screen"
        return "https://aads.newtalk.kr/screenshots/recipe_test.png"

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    monkeypatch.setattr(executor_module, "save_png", fake_save)
    result = await executor_module.BrowserRecipeExecutor()(
        {"action": "snapshot", "risk": "READ", "url": "https://aads.newtalk.kr/ohvis"}
    )

    shot = result["evidence"]["screenshot"]
    assert shot["status"] == "captured"
    assert shot["url"] == "https://aads.newtalk.kr/screenshots/recipe_test.png"


async def test_screenshot_evidence_survives_save_failure(monkeypatch):
    """저장 실패는 증거를 줄일 뿐, 읽기 결과를 실패로 뒤집지 않는다."""
    page = FakePage()

    async def acquire(**kwargs):
        return FakeContext(page), None

    async def failing_save(data, *, prefix=""):
        return None

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    monkeypatch.setattr(executor_module, "save_png", failing_save)
    result = await executor_module.BrowserRecipeExecutor()(
        {"action": "snapshot", "risk": "READ", "url": "https://aads.newtalk.kr/ohvis"}
    )

    assert result["ok"] is True
    assert result["evidence"]["screenshot"]["status"] == "captured"
    assert "url" not in result["evidence"]["screenshot"]


@pytest.mark.parametrize(
    ("error", "expected_route", "expected_reason"),
    [
        ("session expired", "human_gateway", "SESSION_EXPIRED"),
        ("permission denied (403)", "human_gateway", "permission_insufficient"),
        ("net::ERR_NAME_NOT_RESOLVED", "browser_agent", "network_failure"),
    ],
)
def test_smart_browser_recovery_routes_login_permission_and_network_failures(
    error, expected_route, expected_reason
):
    recovery = executor_module.smart_browser_recovery(url="https://aads.newtalk.kr", error=error)

    assert recovery["route"] == expected_route
    assert recovery["reason"] == expected_reason
