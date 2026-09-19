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

    async def screenshot(self):
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
