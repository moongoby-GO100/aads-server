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


class BlockedLocator(FakeLocator):
    async def aria_snapshot(self):
        return "Access Denied"

    async def inner_text(self, **kwargs):
        return "Access Denied\nYou don't have permission to access this resource."


class BlockedPage(FakePage):
    def locator(self, selector):
        self.calls.append(("snapshot", {"selector": selector}))
        return BlockedLocator()


class LongPageLocator(FakeLocator):
    async def inner_text(self, **kwargs):
        # 정상 본문 안에 차단 문구가 섞여 있을 수 있다. 길면 본문으로 본다.
        return ("상품 설명 " * 400) + "판매가 차단되었습니다"


class LongPage(FakePage):
    def locator(self, selector):
        self.calls.append(("snapshot", {"selector": selector}))
        return LongPageLocator()


def test_route_sends_bot_blocked_site_to_pc_lane_without_faking_native_auth():
    """봇 차단 사이트를 native_auth_required 로 위장하지 않고도 PC 레인에 보낸다."""
    route = executor_module.smart_browser_route(
        {"smart_browser": {"native_auth_required": False, "server_access_blocked": True}}
    )

    assert route == {"runtime": "pc_agent", "reason": "server_ip_blocked"}


def test_recovery_routes_bot_block_to_pc_lane_not_human():
    recovery = executor_module.smart_browser_recovery(
        url="https://www.coupang.com/np/search", error="Access Denied"
    )

    assert recovery["route"] == "pc_agent"
    assert recovery["reason"] == "server_ip_blocked"
    assert recovery["resume"] == "same_work_session"


async def test_executor_fails_over_to_pc_lane_when_block_page_is_returned(monkeypatch):
    """차단 화면은 HTTP 200 으로 온다. 단계가 성공해도 레인을 바꿔 다시 시도한다."""
    lanes = []

    async def acquire(**kwargs):
        lanes.append(bool(kwargs.get("browser_work_key")))
        return FakeContext(FakePage() if kwargs.get("browser_work_key") else BlockedPage()), None

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    result = await executor_module.BrowserRecipeExecutor(browser_work_key="ceo-pc")(
        {"action": "snapshot", "risk": "READ", "url": "https://www.coupang.com/np/search"}
    )

    assert result["ok"] is True
    assert lanes == [False, True]
    assert result["route"] == "pc_agent"
    assert result["lane_failover"]["reason"] == "server_ip_blocked"
    assert result["evidence"]["dom"]["text"] == "읽기 전용 대시보드"
    assert "server_access" not in result


async def test_blocked_page_without_pc_lane_is_reported_not_silently_accepted(monkeypatch):
    async def acquire(**kwargs):
        return FakeContext(BlockedPage()), None

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    result = await executor_module.BrowserRecipeExecutor()(
        {"action": "snapshot", "risk": "READ", "url": "https://www.coupang.com/np/search"}
    )

    assert result["ok"] is True
    assert result["server_access"] == {
        "blocked": True, "lane": "browser_agent", "resume": "needs_browser_work_key",
    }


async def test_long_page_containing_block_words_does_not_switch_lane(monkeypatch):
    lanes = []

    async def acquire(**kwargs):
        lanes.append(bool(kwargs.get("browser_work_key")))
        return FakeContext(LongPage()), None

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    result = await executor_module.BrowserRecipeExecutor(browser_work_key="ceo-pc")(
        {"action": "snapshot", "risk": "READ", "url": "https://www.coupang.com/vp/products/1"}
    )

    assert result["ok"] is True
    assert lanes == [False]
    assert result["route"] == "browser_agent"
    assert "lane_failover" not in result
