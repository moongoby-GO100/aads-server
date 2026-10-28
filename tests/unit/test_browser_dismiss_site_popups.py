"""browser_dismiss_site_popups: 허용목록 사이트의 겹친 공지 팝업을 '닫기'로만 위에서부터 닫는다.

① 가짜 페이지(오케스트레이션: 허용목록·상한·재시도·금지 라벨)
② 고정 로컬 HTML 을 실제 Chromium 에 올린 JS 판정(최상단·elementFromPoint·금지 버튼). Chromium 을 못 띄우면 ②만 skip.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from app.api import ceo_chat_tools
from app.services import tool_registry
from app.services.tool_executor import ToolExecutor, current_chat_session_id
from app.services.work_recipe import schema as recipe_schema
from app.services.work_recipe.executor import OverlayNotDismissible
from app.services.work_recipe.schema import (
    OVERLAY_FORBIDDEN_MARKERS,
    SITE_POPUP_ALLOWLIST,
    SITE_POPUP_MAX_CLOSE_LIMIT,
    clamp_site_popup_max_close,
    site_popup_policy_for,
)
from app.services.work_recipe.site_popups import SitePopupRejected, dismiss_site_popups

# ── ① 가짜 페이지 ────────────────────────────────────────────────────────────


class _FakeMouse:
    def __init__(self, page: "_FakePage") -> None:
        self.page = page
        self.clicks: list[tuple[int, int]] = []

    async def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))
        self.page.click_at(x, y)


class _FakePage:
    """popups: 위에서부터. 각 항목 {text, label, hit, dismissible}."""

    def __init__(self, url: str, popups: list[dict[str, Any]]) -> None:
        self.url = url
        self.popups = [dict(p, id=i) for i, p in enumerate(popups)]
        self.mouse = _FakeMouse(self)
        self.pressed_labels: list[str] = []
        self.cleaned = False

    def click_at(self, x: int, y: int) -> None:
        for popup in list(self.popups):
            if (x, y) == (popup["id"], popup["id"] + 1000):
                self.pressed_labels.append(popup["label"])
                if popup.get("dismissible", True):
                    self.popups.remove(popup)

    def _close(self, popup: dict[str, Any]) -> dict[str, Any] | None:
        if popup.get("label") is None:
            return None
        return {"label": popup["label"], "x": popup["id"], "y": popup["id"] + 1000, "hit": popup.get("hit", True)}

    async def evaluate(self, _script: str, args: dict[str, Any] | None = None) -> Any:
        mode = (args or {}).get("mode")
        if mode == "cleanup":
            self.cleaned = True
            return {}
        if mode == "gone":
            return {"gone": all(p["id"] != args["index"] for p in self.popups)}
        if mode == "repoint":
            for popup in self.popups:
                if popup["id"] == args["index"]:
                    return {"close": self._close(popup)}
            return {"close": None}
        return {
            "url": self.url,
            "popups": [
                {
                    "index": p["id"],
                    "title": p.get("title", p["text"][:20]),
                    "text": p["text"],
                    "close": self._close(p),
                }
                for p in self.popups
            ],
        }


@pytest.fixture(autouse=True)
def _approved_seed_list(monkeypatch: pytest.MonkeyPatch):
    """공용 목록(DB)의 approved 시드와 같은 행을 돌려준다 — 허용 도메인은 이제 DB 가 정한다."""
    from app.services.work_recipe import site_overlays

    rows = [
        site_overlays.SiteOverlay(
            id=f"seed-{index}", tenant_id=None, domain="my.a-bly.com",
            root_selector=root, close_selector='button[aria-label="close" i]',
        )
        for index, root in enumerate(['[role="dialog"]', '[role="alertdialog"]', '[role="alert"]'])
    ]

    async def fake_load() -> list:
        return rows

    site_overlays.invalidate_cache()
    monkeypatch.setattr(site_overlays, "_load_approved", fake_load)
    yield
    site_overlays.invalidate_cache()


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.work_recipe import site_popups

    monkeypatch.setattr(site_popups, "_GONE_POLL_SECONDS", 0.0)


def _ably(popups: list[dict[str, Any]]) -> _FakePage:
    return _FakePage("https://my.a-bly.com/dashboard", popups)


async def test_closes_three_modals_and_alert_top_down() -> None:
    page = _ably([
        {"text": "알림: 연동하기 안내 계정을 연결하세요", "label": "닫기"},
        {"text": "공지 3 점검 안내", "label": "닫기"},
        {"text": "공지 2 정산 일정", "label": "Close"},
        {"text": "공지 1 신규 기능", "label": "닫기"},
    ])
    result = await dismiss_site_popups(page)
    assert result["status"] == "closed"
    assert result["closed_count"] == 4 and result["remaining_count"] == 0
    assert result["closed_popups"] == [
        "알림: 연동하기 안내 계정을 연결하세요", "공지 3 점검 안내", "공지 2 정산 일정", "공지 1 신규 기능",
    ]
    assert result["remaining_reason"] == ""
    assert page.pressed_labels == ["닫기", "닫기", "Close", "닫기"]
    assert page.cleaned


async def test_reported_text_is_first_40_chars() -> None:
    long_text = "가" * 100
    result = await dismiss_site_popups(_ably([{"text": long_text, "label": "닫기"}]))
    assert result["closed_popups"] == ["가" * 40]


@pytest.mark.parametrize(
    "label",
    ["하루 동안 보지 않기", "다시 보지 않기", "신청하기", "연동하기", "확인", "동의", "저장", "결제하기", "Don't show again", "Confirm"],
)
async def test_forbidden_buttons_are_never_pressed(label: str) -> None:
    page = _ably([{"text": "공지", "label": label}])
    result = await dismiss_site_popups(page)
    assert page.mouse.clicks == []
    assert result["status"] == "blocked"
    assert result["closed_count"] == 0 and result["remaining_count"] == 1
    assert result["remaining_reason"] == "close_control_missing"


async def test_skips_forbidden_popup_but_closes_the_others() -> None:
    page = _ably([
        {"text": "공지 A", "label": "하루 동안 보지 않기"},
        {"text": "공지 B", "label": "닫기"},
    ])
    result = await dismiss_site_popups(page)
    assert page.pressed_labels == ["닫기"]
    assert result["closed_count"] == 1 and result["remaining_count"] == 1
    assert result["status"] == "partial"
    assert result["remaining_reason"] == "close_control_missing"


async def test_obscured_close_is_never_clicked() -> None:
    page = _ably([{"text": "공지", "label": "닫기", "hit": False}])
    result = await dismiss_site_popups(page)
    assert page.mouse.clicks == []
    assert result["status"] == "blocked"
    assert result["remaining_reason"] == "close_obscured"


async def test_consent_or_security_popup_is_left_alone() -> None:
    page = _ably([{"text": "개인정보 수집 동의", "title": "개인정보 수집 동의", "label": "닫기"}])
    result = await dismiss_site_popups(page)
    assert page.mouse.clicks == []
    assert result["remaining_reason"] == "sensitive_popup"


async def test_host_outside_allowlist_is_rejected_without_touching_page() -> None:
    page = _FakePage("https://evil.example.com/x", [{"text": "공지", "label": "닫기"}])
    with pytest.raises(SitePopupRejected) as exc:
        await dismiss_site_popups(page)
    assert exc.value.code == "site_not_allowlisted"
    assert page.mouse.clicks == [] and len(page.popups) == 1


async def test_lookalike_host_is_rejected() -> None:
    for url in ("https://my.a-bly.com.evil.io/", "https://notmy.a-bly.com.example/", "about:blank"):
        with pytest.raises(SitePopupRejected):
            await dismiss_site_popups(_FakePage(url, []))


async def test_non_dismissible_popup_retries_once_then_overlay_not_dismissible() -> None:
    page = _ably([
        {"text": "공지 2", "label": "닫기"},
        {"text": "고장난 공지", "label": "닫기", "dismissible": False},
    ])
    with pytest.raises(OverlayNotDismissible) as exc:
        await dismiss_site_popups(page)
    assert exc.value.reason == "not_closed_after_click"
    assert [d["text"] for d in exc.value.dismissed] == ["공지 2"]
    assert exc.value.remaining_count == 1
    assert page.mouse.clicks.count((1, 1001)) == 2  # 최초 1회 + 재시도 1회
    assert page.cleaned


async def test_max_close_caps_the_number_of_closes() -> None:
    page = _ably([{"text": f"공지 {i}", "label": "닫기"} for i in range(6)])
    result = await dismiss_site_popups(page, max_close=2)
    assert result["closed_count"] == 2 and result["remaining_count"] == 4
    assert result["remaining_reason"] == "max_close_reached"
    assert result["status"] == "partial"
    assert len(page.mouse.clicks) == 2


async def test_max_close_is_clamped_to_hard_limit() -> None:
    page = _ably([{"text": f"공지 {i}", "label": "닫기"} for i in range(25)])
    result = await dismiss_site_popups(page, max_close=999)
    assert result["closed_count"] == SITE_POPUP_MAX_CLOSE_LIMIT == 20
    assert result["remaining_count"] == 5


def test_clamp_values() -> None:
    assert clamp_site_popup_max_close(None) == 10
    assert clamp_site_popup_max_close("abc") == 10
    assert clamp_site_popup_max_close(0) == 1
    assert clamp_site_popup_max_close(-5) == 1
    assert clamp_site_popup_max_close(7) == 7
    assert clamp_site_popup_max_close(100) == 20


async def test_no_popups_is_a_no_op() -> None:
    page = _ably([])
    result = await dismiss_site_popups(page)
    assert result["status"] == "none" and result["closed_count"] == 0 and result["remaining_count"] == 0
    assert page.mouse.clicks == []


def test_allowlist_initial_value_and_matching() -> None:
    assert set(SITE_POPUP_ALLOWLIST) == {"my.a-bly.com"}
    assert site_popup_policy_for("my.a-bly.com") is not None
    assert site_popup_policy_for("MY.A-BLY.COM.") is not None
    assert site_popup_policy_for("a-bly.com") is None
    assert site_popup_policy_for("evil-my.a-bly.com.evil.io") is None
    assert site_popup_policy_for("") is None


def test_forbidden_markers_cover_site_popup_phrases() -> None:
    for phrase in ("하루 동안 보지 않기", "다시 보지 않기", "신청", "연동하기", "확인", "동의", "결제", "저장"):
        assert OVERLAY_FORBIDDEN_MARKERS.search(phrase), phrase
    for phrase in ("닫기", "Close", "×"):
        assert not OVERLAY_FORBIDDEN_MARKERS.search(phrase), phrase


def test_recipe_overlay_text_check_is_unchanged() -> None:
    assert recipe_schema.OVERLAY_SENSITIVE_MARKERS.search("결제")
    assert not recipe_schema.OVERLAY_SENSITIVE_MARKERS.search("연동 안내를 확인하세요")


# ── 도구 레이어 ───────────────────────────────────────────────────────────────


class _FakeCtx:
    def __init__(self, page: Any) -> None:
        self.pages = [page]


@pytest.fixture
def acquire(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[Any, ...]] = []

    def _apply(page: Any) -> list[tuple[Any, ...]]:
        async def fake_acquire(session_id: str = "", work_key: str = "", url: str = "about:blank", prefer_headless: bool = True, browser_lane: str = ""):
            seen.append((session_id, work_key, browser_lane))
            return _FakeCtx(page), None

        monkeypatch.setattr(ceo_chat_tools, "_acquire_pw_context", fake_acquire)
        return seen

    return _apply


async def test_tool_returns_json_and_audit_log(acquire, caplog) -> None:
    page = _ably([{"text": "공지 1", "label": "닫기"}, {"text": "공지 0", "label": "닫기"}])
    seen = acquire(page)
    token = current_chat_session_id.set("65f926cb-chat")
    try:
        with caplog.at_level(logging.INFO):
            out = await ceo_chat_tools.tool_browser_dismiss_site_popups("ably-admin", max_close=5)
    finally:
        current_chat_session_id.reset(token)
    data = json.loads(out)
    assert data["closed_count"] == 2 and data["remaining_count"] == 0
    assert seen == [("", "ably-admin", "server")]
    audit = [r.getMessage() for r in caplog.records if "browser_dismiss_site_popups_audit" in r.getMessage()]
    assert len(audit) == 1 and "work_key=ably-admin" in audit[0] and "closed=2" in audit[0]
    assert "chat_session=65f926cb-chat" in audit[0]


async def test_tool_rejects_site_not_allowlisted(acquire) -> None:
    page = _FakePage("https://example.com/", [{"text": "공지", "label": "닫기"}])
    acquire(page)
    out = await ceo_chat_tools.tool_browser_dismiss_site_popups("ably-admin")
    assert out.startswith("[ERROR]") and "site_not_allowlisted" in out
    assert page.mouse.clicks == []


@pytest.mark.parametrize("kwargs", [{"browser_lane": "pc"}, {"browser_session_id": "sess-1"}])
async def test_tool_rejects_pc_lane(acquire, kwargs) -> None:
    page = _ably([{"text": "공지", "label": "닫기"}])
    seen = acquire(page)
    out = await ceo_chat_tools.tool_browser_dismiss_site_popups("ably-admin", **kwargs)
    assert "pc_lane_unsupported" in out and seen == [] and page.mouse.clicks == []


async def test_tool_requires_work_key(acquire) -> None:
    seen = acquire(_ably([]))
    out = await ceo_chat_tools.tool_browser_dismiss_site_popups("")
    assert "work_key_required" in out and seen == []


async def test_tool_reports_overlay_not_dismissible(acquire) -> None:
    acquire(_ably([{"text": "고장", "label": "닫기", "dismissible": False}]))
    out = await ceo_chat_tools.tool_browser_dismiss_site_popups("ably-admin")
    assert out.startswith("[ERROR]") and "overlay_not_dismissible" in out
    payload = json.loads(out.split(": ", 1)[1])
    assert payload["reason"] == "not_closed_after_click" and payload["remaining_count"] == 1


async def test_executor_dispatch_routes_to_tool(acquire) -> None:
    page = _ably([{"text": "공지", "label": "닫기"}])
    acquire(page)
    out = await ToolExecutor().execute("browser_dismiss_site_popups", {"browser_work_key": "ably-admin", "max_close": 3})
    assert "closed_count" in str(out) and not page.popups


async def test_cookies_and_storage_are_never_touched() -> None:
    """페이지에 쿠키·저장소 API 접점이 없다 — 가짜 페이지가 evaluate/mouse 외를 부르면 AttributeError."""
    page = _ably([{"text": "공지", "label": "닫기"}])
    assert not hasattr(page, "context") and not hasattr(page, "add_cookies")
    await dismiss_site_popups(page)


def test_tool_registered_everywhere() -> None:
    from app.services import tool_executor

    name = "browser_dismiss_site_popups"
    assert name in {t["name"] for t in ceo_chat_tools.TOOL_DEFINITIONS}
    assert name in tool_registry._TOOLS
    assert name in tool_executor._BROWSER_TOOLS
    assert name in tool_executor._INTENT_TOOL_MAP["browser"]
    assert name in tool_registry.INTENT_REQUIRED_TOOLS["browser"]
    assert name in tool_registry._GROUPS["browser"]
    schema = tool_registry._TOOLS[name]["input_schema"]
    assert schema["required"] == ["browser_work_key"]
    assert schema["properties"]["max_close"]["maximum"] == 20


# ── ② 실제 Chromium + 고정 HTML ──────────────────────────────────────────────

_FIXED_HTML = """<!doctype html><html lang="ko"><body style="margin:0">
<button id="logout" style="position:absolute;left:300px;top:300px;width:200px;height:60px"
        onclick="window.__log.push('logout')">로그아웃</button>
<div id="mount"></div>
<script>
window.__log = [];
function popup(id, role, z, text, buttons, left, top) {
  const d = document.createElement('div');
  d.id = id; d.setAttribute('role', role);
  d.style.cssText = `position:fixed;left:${left}px;top:${top}px;width:360px;height:220px;background:#fff;border:1px solid #333;z-index:${z}`;
  d.innerHTML = `<p>${text}</p>`;
  for (const [label, kind] of buttons) {
    const b = document.createElement('button');
    b.textContent = label;
    b.style.cssText = 'margin:4px;padding:6px 10px';
    b.onclick = () => {
      window.__log.push(label + '@' + id);
      if (kind === 'close') d.style.display = 'none';
      if (kind === 'hide') d.style.display = 'none';
    };
    d.appendChild(b);
  }
  document.getElementById('mount').appendChild(d);
}
popup('m1', 'dialog', 1001, '공지 1 신규 기능 안내', [['하루 동안 보지 않기', 'hide'], ['닫기', 'close']], 100, 100);
popup('m2', 'dialog', 1002, '공지 2 정산 일정 안내', [['하루 동안 보지 않기', 'hide'], ['닫기', 'close']], 110, 110);
popup('m3', 'dialog', 1003, '공지 3 점검 안내', [['하루 동안 보지 않기', 'hide'], ['닫기', 'close']], 120, 120);
popup('a1', 'alert', 2000, '알림 계정 연동 필요', [['연동하기', 'none'], ['닫기', 'close']], 130, 130);
const hiddenClose = document.createElement('button');
hiddenClose.textContent = 'Close';
hiddenClose.style.cssText = 'width:0;height:0;padding:0;border:0;overflow:hidden';
document.getElementById('m1').appendChild(hiddenClose);
const cb = document.createElement('input'); cb.type = 'checkbox'; cb.id = 'dontshow';
cb.onchange = () => window.__log.push('checkbox');
document.getElementById('m3').appendChild(cb);
</script></body></html>"""

_STUCK_HTML = _FIXED_HTML.replace("if (kind === 'close') d.style.display = 'none';", "")


@pytest.fixture
async def chromium_page():
    playwright_api = pytest.importorskip("playwright.async_api")
    pw = await playwright_api.async_playwright().start()
    try:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    except Exception as exc:  # noqa: BLE001
        await pw.stop()
        pytest.skip(f"서버 Playwright Chromium 기동 실패 — 실제 DOM 미검증: {str(exc)[:80]}")
    try:
        context = await browser.new_context(viewport={"width": 800, "height": 600})
        page = await context.new_page()
        yield page
    finally:
        await browser.close()
        await pw.stop()


async def _open(page: Any, html: str, url: str = "https://my.a-bly.com/dashboard") -> None:
    async def fulfill(route: Any) -> None:
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    await page.route("**/*", fulfill)
    await page.goto(url)


async def test_real_dom_closes_stacked_modals_and_alert_without_forbidden_clicks(chromium_page) -> None:
    page = chromium_page
    await _open(page, _FIXED_HTML)
    result = await dismiss_site_popups(page)

    log = await page.evaluate("window.__log")
    assert log == ["닫기@a1", "닫기@m3", "닫기@m2", "닫기@m1"]
    assert result["status"] == "closed"
    assert result["closed_count"] == 4 and result["remaining_count"] == 0
    assert [t[:5] for t in result["closed_popups"]] == ["알림 계정", "공지 3 ", "공지 2 ", "공지 1 "]
    assert all(len(t) <= 40 for t in result["closed_popups"])
    assert not any("하루" in entry or "연동" in entry or entry == "checkbox" for entry in log)
    assert await page.evaluate("typeof window.__aadsSitePopups") == "undefined"
    # 로그아웃 버튼은 건드리지 않았다
    assert "logout" not in log


async def test_real_dom_logout_is_clickable_after_dismiss_but_obscured_before(chromium_page) -> None:
    page = chromium_page
    await _open(page, _FIXED_HTML)
    assert await page.evaluate("document.elementFromPoint(400, 330).id") != "logout"
    await dismiss_site_popups(page)
    assert await page.evaluate("document.elementFromPoint(400, 330).id") == "logout"


async def test_real_dom_max_close_stops_after_two(chromium_page) -> None:
    page = chromium_page
    await _open(page, _FIXED_HTML)
    result = await dismiss_site_popups(page, max_close=2)
    assert result["closed_count"] == 2 and result["remaining_count"] == 2
    assert result["remaining_reason"] == "max_close_reached"
    assert await page.evaluate("window.__log") == ["닫기@a1", "닫기@m3"]


async def test_real_dom_stuck_modal_is_overlay_not_dismissible(chromium_page) -> None:
    page = chromium_page
    await _open(page, _STUCK_HTML)
    with pytest.raises(OverlayNotDismissible) as exc:
        await dismiss_site_popups(page)
    assert exc.value.reason == "not_closed_after_click"
    assert exc.value.dismissed == []
    log = await page.evaluate("window.__log")
    assert log == ["닫기@a1", "닫기@a1"]  # 1회 재시도 후 중단
    assert not any("하루" in entry for entry in log)


async def test_real_dom_host_outside_allowlist_does_not_click(chromium_page) -> None:
    page = chromium_page
    await _open(page, _FIXED_HTML, url="https://example.com/dashboard")
    with pytest.raises(SitePopupRejected):
        await dismiss_site_popups(page)
    assert await page.evaluate("window.__log") == []


async def test_real_dom_modal_with_only_forbidden_buttons_is_left_open(chromium_page) -> None:
    page = chromium_page
    html = _FIXED_HTML.replace("[['하루 동안 보지 않기', 'hide'], ['닫기', 'close']], 100, 100", "[['하루 동안 보지 않기', 'hide']], 100, 100")
    await _open(page, html)
    result = await dismiss_site_popups(page)
    log = await page.evaluate("window.__log")
    assert log == ["닫기@a1", "닫기@m3", "닫기@m2"]
    assert result["closed_count"] == 3 and result["remaining_count"] == 1
    assert result["remaining_reason"] == "close_control_missing"
    assert result["status"] == "partial"
