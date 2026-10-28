"""CEO 채팅 브라우저 도구: 클릭 신뢰성 / 스냅샷 경합 / 스크린샷 참조 격리 / 날짜 필터 (AADS-SB-PC-INTEGRITY-20261028).

서버 Playwright(실제 Chromium) 항목은 안전한 로컬 HTML 만 사용하며, 브라우저를 띄울 수 없으면 해당 테스트만 skip 된다.
"""
from __future__ import annotations

import base64
import datetime as dt
import http.server
import json
import re
import threading
from unittest.mock import AsyncMock

import pytest

from app.api import ceo_chat_tools as cct
from app.browser_bridge.models import BrowserBridgeSession, BrowserEndpoint, BrowserEndpointKind, utcnow
from app.browser_bridge.service import BrowserBridgeError, BrowserBridgeService, _LocalAgentPage
from app.services import browser_artifact_state as artifacts
from app.services import browser_login_autosave as autosave

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# ── 날짜 필터 ─────────────────────────────────────────────────────────

class TestHistoryDateRange:
    def test_valid_range(self):
        start, end, err = cct._parse_history_date_range("2026-10-01", "2026-10-28")
        assert (start, end, err) == (dt.date(2026, 10, 1), dt.date(2026, 10, 28), "")

    def test_open_ended_and_blank(self):
        assert cct._parse_history_date_range("", "") == (None, None, "")
        assert cct._parse_history_date_range("2026-10-01", None)[0] == dt.date(2026, 10, 1)
        assert cct._parse_history_date_range("   ", "2026-10-02")[1] == dt.date(2026, 10, 2)

    @pytest.mark.parametrize("bad", ["2026-13-01", "2026-02-30", "10/28/2026", "2026-1-5", "yesterday", "20261028", "2026-10-28T00:00"])
    def test_invalid_format_or_calendar_date(self, bad):
        _, _, err = cct._parse_history_date_range(bad, "")
        assert err.startswith("[ERROR] date_from")
        _, _, err = cct._parse_history_date_range("", bad)
        assert err.startswith("[ERROR] date_to")

    def test_reversed_range_is_error(self):
        _, _, err = cct._parse_history_date_range("2026-10-28", "2026-10-01")
        assert "범위 오류" in err

    def test_same_day_is_allowed(self):
        assert cct._parse_history_date_range("2026-10-28", "2026-10-28")[2] == ""


class _FakeConn:
    def __init__(self, fts_rows, like_rows):
        self.queries: list[tuple[str, tuple]] = []
        self._replies = [fts_rows, like_rows]
        self.closed = False

    async def fetch(self, sql, *params):
        self.queries.append((sql, params))
        return self._replies[min(len(self.queries) - 1, len(self._replies) - 1)]

    async def close(self):
        self.closed = True


def _row(content="결제 내용"):
    return {"id": 1, "role": "user", "content": content, "created_at": dt.datetime(2026, 10, 20, 9, 30), "session_name": "S"}


class TestSearchChatHistoryDates:
    @pytest.mark.asyncio
    async def test_invalid_date_never_touches_the_database(self, monkeypatch):
        connect = AsyncMock()
        monkeypatch.setattr(cct.asyncpg, "connect", connect)
        out = await cct.tool_search_chat_history("q", "dsn", date_from="2026-02-31")
        assert out.startswith("[ERROR] date_from")
        out = await cct.tool_search_chat_history("q", "dsn", date_from="2026-10-28", date_to="2026-10-01")
        assert "범위 오류" in out
        connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_fts_and_like_share_the_same_date_params_as_date_objects(self, monkeypatch):
        conn = _FakeConn(fts_rows=[], like_rows=[_row()])
        monkeypatch.setattr(cct.asyncpg, "connect", AsyncMock(return_value=conn))
        out = await cct.tool_search_chat_history("결제", "dsn", date_from="2026-10-01", date_to="2026-10-28")
        assert out.startswith("[LIKE 검색]")
        assert len(conn.queries) == 2
        (fts_sql, fts_params), (like_sql, like_params) = conn.queries
        assert "plainto_tsquery" in fts_sql and "ILIKE" in like_sql
        assert fts_params[1:] == like_params[1:] == (dt.date(2026, 10, 1), dt.date(2026, 10, 28))
        for params in (fts_params, like_params):
            assert all(not isinstance(p, str) for p in params[1:])
        assert re.search(r"created_at >= \$2::date", fts_sql) and re.search(r"created_at >= \$2::date", like_sql)
        assert conn.closed is True

    @pytest.mark.asyncio
    async def test_fts_hit_with_only_date_to_binds_a_single_date(self, monkeypatch):
        conn = _FakeConn(fts_rows=[_row()], like_rows=[])
        monkeypatch.setattr(cct.asyncpg, "connect", AsyncMock(return_value=conn))
        out = await cct.tool_search_chat_history("결제", "dsn", date_to="2026-10-28")
        assert out.startswith("[FTS 검색]")
        assert conn.queries[0][1][1:] == (dt.date(2026, 10, 28),)

    @pytest.mark.asyncio
    async def test_no_date_filter_adds_no_date_condition(self, monkeypatch):
        conn = _FakeConn(fts_rows=[_row()], like_rows=[])
        monkeypatch.setattr(cct.asyncpg, "connect", AsyncMock(return_value=conn))
        await cct.tool_search_chat_history("결제", "dsn")
        assert "::date" not in conn.queries[0][0]
        assert len(conn.queries[0][1]) == 1


# ── PC 레인 도구 메시지 ────────────────────────────────────────────────

def _pc_page(**metadata) -> _LocalAgentPage:
    session = BrowserBridgeSession(
        session_id="sess-1", label="t",
        endpoint=BrowserEndpoint(kind=BrowserEndpointKind.LOCAL_AGENT, metadata={"agent_id": "pc-1", "port": "9222", **metadata}),
        registered_at=utcnow(), work_key="wk-a",
    )
    return _LocalAgentPage(session, BrowserBridgeService())


@pytest.fixture
def use_page(monkeypatch):
    def install(page):
        async def acquire(*_a, **_kw):
            return type("Ctx", (), {"pages": [page]})(), ""

        monkeypatch.setattr(cct, "_acquire_pw_context", acquire)
        monkeypatch.setattr(autosave, "on_submit", AsyncMock(return_value=""))
        return page

    return install


class TestPcClickMessages:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state,changed,marker", [
        ("no_observed_effect", [], "[클릭 전달됨·변화 없음 — 미검증]"),
        ("observed_change", ["text"], "[클릭 전달됨·변화 관측]"),
        ("unknown", [], "[클릭 전달됨·미검증]"),
    ])
    async def test_unverified_click_is_never_reported_as_complete(self, use_page, monkeypatch, state, changed, marker):
        page = use_page(_pc_page())
        monkeypatch.setattr(page, "click_detailed", AsyncMock(return_value={
            "dispatched": True, "verified": False, "verification_state": state, "changed": changed,
            "href_before": "https://a.example/x", "href_after": "https://a.example/x",
        }))
        out = await cct.tool_browser_click("#save", browser_session_id="sess-1")
        assert marker in out
        assert "[클릭 완료]" not in out

    @pytest.mark.asyncio
    async def test_verified_click_reports_complete_with_checks(self, use_page, monkeypatch):
        page = use_page(_pc_page())
        monkeypatch.setattr(page, "click_detailed", AsyncMock(return_value={
            "dispatched": True, "verified": True, "verification_state": "verified",
            "checks": {"text_present": True}, "href_before": "https://a.example/x", "href_after": "https://a.example/y",
        }))
        out = await cct.tool_browser_click("#save", browser_session_id="sess-1", expect={"text_present": "저장됨"})
        assert out.startswith("[클릭 완료]") and "text_present" in out and "https://a.example/y" in out
        page.click_detailed.assert_awaited_once()
        assert page.click_detailed.await_args.kwargs["expect"] == {"text_present": "저장됨"}

    @pytest.mark.asyncio
    async def test_not_verified_error_says_no_auto_reclick(self, use_page, monkeypatch):
        page = use_page(_pc_page())
        monkeypatch.setattr(page, "click_detailed", AsyncMock(side_effect=BrowserBridgeError(
            "x", error_code="CLICK_NOT_VERIFIED", detail={"dispatched": True, "checks": {"text_present": False}},
        )))
        out = await cct.tool_browser_click("#save", browser_session_id="sess-1", expect={"text_present": "저장됨"})
        assert out.startswith("[ERROR]") and "CLICK_NOT_VERIFIED" in out and "다시 클릭하지 않았습니다" in out
        assert page.click_detailed.await_count == 1

    @pytest.mark.asyncio
    async def test_uncertain_dispatch_is_not_retried(self, use_page, monkeypatch):
        page = use_page(_pc_page())
        monkeypatch.setattr(page, "click_detailed", AsyncMock(side_effect=BrowserBridgeError(
            "timeout", error_code="COMMAND_TIMEOUT", detail={"dispatch_state": "uncertain", "retry_safe": False},
        )))
        out = await cct.tool_browser_click("#pay", browser_session_id="sess-1")
        assert "불확실" in out and "재시도하지 않았습니다" in out
        assert page.click_detailed.await_count == 1

    @pytest.mark.asyncio
    async def test_target_mismatch_is_reported_as_error(self, use_page, monkeypatch):
        page = use_page(_pc_page())
        monkeypatch.setattr(page, "click_detailed", AsyncMock(side_effect=BrowserBridgeError(
            "다른 탭", error_code="TARGET_MISMATCH", detail={"dispatch_state": "uncertain", "retry_safe": False},
        )))
        out = await cct.tool_browser_click("#x", browser_session_id="sess-1")
        assert "TARGET_MISMATCH" in out and "[클릭 완료]" not in out

    @pytest.mark.asyncio
    async def test_invalid_arguments_are_rejected_before_acquiring_a_page(self, monkeypatch):
        acquire = AsyncMock()
        monkeypatch.setattr(cct, "_acquire_pw_context", acquire)
        assert "INVALID_EXPECT" in await cct.tool_browser_click("#x", expect={"bogus": 1})
        assert "INVALID_NTH" in await cct.tool_browser_click("#x", nth="abc")
        assert (await cct.tool_browser_click("  ")).startswith("[ERROR] selector")
        acquire.assert_not_called()


class TestPcSnapshot:
    @pytest.mark.asyncio
    async def test_observation_has_real_href_target_and_states_it_is_not_aria(self, use_page, monkeypatch):
        page = use_page(_pc_page())
        monkeypatch.setattr(page, "observe", AsyncMock(return_value={
            "href": "https://shop.example/checkout", "title": "결제", "ready": "complete", "stable": True, "attempts": 2,
            "target_id": "TAB-A", "text": "주문 요약",
            "controls": [
                {"tag": "input", "type": "checkbox", "name": "약관 동의", "checked": True},
                {"tag": "select", "name": "배송", "selected_option": "택배"},
                {"tag": "button", "name": "결제하기", "disabled": True},
                {"tag": "input", "type": "password", "name": "비밀번호", "secret": True, "filled": True},
            ],
            "dialogs": [{"role": "dialog", "name": "확인", "text": "정말 진행할까요?"}],
        }))
        out = await cct.tool_browser_snapshot(browser_session_id="sess-1")
        assert out.startswith("[페이지 관측 — https://shop.example/checkout]")
        assert "URL: https://shop.example/checkout" in out
        assert "target_id=TAB-A" in out and "읽기 시도=2" in out
        assert "ARIA 접근성 트리가 아닙니다" in out
        assert "checked=true" in out and 'option="택배"' in out and "disabled" in out
        assert "[filled]" in out and "열린 대화상자 1개" in out
        assert not out.startswith("[ARIA 스냅샷")

    @pytest.mark.asyncio
    async def test_unstable_navigation_is_an_explicit_failure(self, use_page, monkeypatch):
        page = use_page(_pc_page())
        monkeypatch.setattr(page, "observe", AsyncMock(side_effect=BrowserBridgeError("x", error_code="SNAPSHOT_UNSTABLE")))
        out = await cct.tool_browser_snapshot(browser_session_id="sess-1")
        assert out.startswith("[ERROR] 스냅샷 실패 (SNAPSHOT_UNSTABLE)")


# ── 서버 레인 스냅샷 경합(결정적 가짜 페이지) ───────────────────────────

class _RacyPage:
    def __init__(self, failures):
        self.failures = failures
        self.url = "https://shop.example/final"
        self.reads = 0
        self.waits = 0

    async def title(self):
        self.reads += 1
        if self.reads <= self.failures:
            raise RuntimeError("Page.title: Execution context was destroyed, most likely because of a navigation")
        return "Final"

    async def evaluate(self, *_a, **_kw):
        return []

    def locator(self, _sel):
        page = self

        class _Loc:
            async def aria_snapshot(self):
                return '- heading "Final"\n- button "구매"'

        return _Loc()

    async def wait_for_load_state(self, *_a, **_kw):
        self.waits += 1


class TestServerSnapshotNavigationRace:
    @pytest.mark.asyncio
    async def test_retries_and_reports_the_url_after_navigation(self, use_page):
        page = use_page(_RacyPage(failures=2))
        out = await cct.tool_browser_snapshot()
        assert page.reads == 3 and page.waits == 2
        assert out.startswith("[ARIA 스냅샷 — https://shop.example/final]")
        assert '- button "구매"' in out

    @pytest.mark.asyncio
    async def test_gives_explicit_failure_when_navigation_never_settles(self, use_page, monkeypatch):
        monkeypatch.setattr(cct, "_SNAPSHOT_RETRY_SECONDS", 0.0)
        page = use_page(_RacyPage(failures=99))
        out = await cct.tool_browser_snapshot()
        assert out.startswith("[ERROR] 스냅샷 실패 (SNAPSHOT_UNSTABLE)")
        assert page.reads == cct._SNAPSHOT_READ_ATTEMPTS

    @pytest.mark.asyncio
    async def test_non_navigation_errors_are_not_retried(self, use_page):
        page = use_page(_RacyPage(failures=0))

        async def boom():
            raise RuntimeError("renderer crashed")

        page.title = boom
        out = await cct.tool_browser_snapshot()
        assert out.startswith("[ERROR] 스냅샷 실패:") and "renderer crashed" in out


# ── 스크린샷 참조 접근 격리 ─────────────────────────────────────────────

@pytest.fixture
def artifact_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "SCREENSHOT_ARTIFACT_DIR", tmp_path / "art")
    return tmp_path / "art"


def _save(**kw):
    base = dict(png=PNG, tenant_id="tenant-1", session_id="sess-1", work_key="wk-a", url="https://a.example/p?token=SECRET#frag", target_id="TAB-A")
    base.update(kw)
    return artifacts.save_screenshot_artifact(**base)


class TestScreenshotArtifactIsolation:
    def test_same_tenant_session_work_key_can_read(self, artifact_dir):
        ref = _save()
        png, meta = artifacts.load_screenshot_artifact(
            artifact_id=ref["artifact_id"], tenant_id="tenant-1", session_id="sess-1", work_key="wk-a",
        )
        assert png == PNG and meta["target_id"] == "TAB-A"

    @pytest.mark.parametrize("kwargs", [
        {"tenant_id": "tenant-2"},
        {"tenant_id": ""},
        {"tenant_id": "tenant-1", "session_id": "sess-OTHER"},
        {"tenant_id": "tenant-1", "work_key": "wk-OTHER"},
    ])
    def test_other_tenant_session_or_work_key_is_denied_as_not_found(self, artifact_dir, kwargs):
        ref = _save()
        with pytest.raises(artifacts.ArtifactError) as err:
            artifacts.load_screenshot_artifact(artifact_id=ref["artifact_id"], **kwargs)
        assert err.value.code == "not_found"

    def test_unknown_traversal_and_malformed_ids_are_not_found(self, artifact_dir):
        _save()
        for bad in ("A" * 32, "../../etc/passwd", "..%2f..%2fx", "", "short"):
            with pytest.raises(artifacts.ArtifactError) as err:
                artifacts.load_screenshot_artifact(artifact_id=bad, tenant_id="tenant-1")
            assert err.value.code == "not_found"

    def test_tampered_file_is_rejected(self, artifact_dir):
        ref = _save()
        (artifact_dir / f"{ref['artifact_id']}.png").write_bytes(PNG + b"tamper")
        with pytest.raises(artifacts.ArtifactError):
            artifacts.load_screenshot_artifact(artifact_id=ref["artifact_id"], tenant_id="tenant-1")

    def test_expired_artifact_is_not_found(self, artifact_dir, monkeypatch):
        ref = _save()
        monkeypatch.setattr(artifacts, "SCREENSHOT_ARTIFACT_TTL_SECONDS", -1)
        with pytest.raises(artifacts.ArtifactError):
            artifacts.load_screenshot_artifact(artifact_id=ref["artifact_id"], tenant_id="tenant-1")

    def test_tenant_is_required_and_non_png_is_refused(self, artifact_dir):
        with pytest.raises(artifacts.ArtifactError) as err:
            _save(tenant_id="")
        assert err.value.code == "tenant_required"
        with pytest.raises(artifacts.ArtifactError) as err:
            _save(png=b"<html>not an image</html>")
        assert err.value.code == "not_png"

    def test_url_is_redacted_in_reference_and_metadata(self, artifact_dir):
        ref = _save()
        assert ref["url"] == "https://a.example/p"
        stored = (artifact_dir / f"{ref['artifact_id']}.json").read_text()
        assert "SECRET" not in stored and "frag" not in stored

    def test_files_are_owner_only(self, artifact_dir):
        ref = _save()
        assert (artifact_dir / f"{ref['artifact_id']}.png").stat().st_mode & 0o077 == 0

    @pytest.mark.asyncio
    async def test_tool_returns_reference_without_image_body(self, use_page, artifact_dir, monkeypatch):
        page = use_page(_pc_page())
        monkeypatch.setattr(page, "screenshot", AsyncMock(return_value=PNG))
        page.url = "https://a.example/p?token=SECRET#frag"
        page.last_screenshot_target_id = "TAB-A"
        out = await cct.tool_browser_screenshot(browser_session_id="sess-1", browser_work_key="wk-a", tenant_id="tenant-1")
        assert out.startswith("[스크린샷 참조 — 접근 통제 아티팩트]")
        assert "target_id=TAB-A" in out
        assert "SECRET" not in out and "frag" not in out
        assert base64.b64encode(PNG).decode()[:20] not in out and "data:image" not in out and "iVBOR" not in out
        artifact_id = re.search(r"REF: /api/v1/browser-tasks/artifacts/([\w-]+)", out).group(1)
        assert artifacts.load_screenshot_artifact(artifact_id=artifact_id, tenant_id="tenant-1")[0] == PNG
        with pytest.raises(artifacts.ArtifactError):
            artifacts.load_screenshot_artifact(artifact_id=artifact_id, tenant_id="tenant-2")

    @pytest.mark.asyncio
    async def test_tool_refuses_without_tenant(self, use_page, artifact_dir, monkeypatch):
        page = use_page(_pc_page())
        monkeypatch.setattr(page, "screenshot", AsyncMock(return_value=PNG))
        out = await cct.tool_browser_screenshot(browser_session_id="sess-1", tenant_id="")
        assert out.startswith("[ERROR]") and "tenant_required" in out
        assert not artifact_dir.exists() or not list(artifact_dir.glob("*.png"))

    @pytest.mark.asyncio
    async def test_screenshot_does_not_navigate(self, use_page, artifact_dir, monkeypatch):
        page = use_page(_pc_page())
        monkeypatch.setattr(page, "screenshot", AsyncMock(return_value=PNG))
        monkeypatch.setattr(page, "goto", AsyncMock())
        await cct.tool_browser_screenshot(browser_session_id="sess-1", tenant_id="tenant-1")
        page.goto.assert_not_called()


# ── 실제 서버 Playwright(Chromium) — 안전한 로컬 페이지 ─────────────────────

_SAFE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>안전한 테스트 페이지</title></head><body>
<h1 id="who">READY</h1>
<button id="save" onclick="document.getElementById('who').textContent='SAVED'">저장</button>
<button id="noop">변화없음</button>
<button id="dis" disabled>비활성</button>
<button class="dup">중복</button><button class="dup">중복</button>
<button id="hid" style="display:none">숨김</button>
<div id="wrapper">삭제 안내문</div>
<div class="card"><button id="confirm" onclick="document.getElementById('who').textContent='CONFIRMED'">확정</button></div>
<label><input type="checkbox" id="agree"> 동의</label>
<input type="password" id="pw" aria-label="비밀번호" value="S3cr3t-Value">
<div style="position:relative;height:40px"><button id="covered" style="position:absolute;left:0;top:0;width:120px;height:30px">가려짐</button>
<div style="position:absolute;left:0;top:0;width:120px;height:30px;background:#ccc"></div></div>
</body></html>"""


class _SafeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = _SAFE_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def safe_site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SafeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/safe?token=SECRET#frag"
    server.shutdown()


@pytest.fixture
async def real_page(safe_site, use_page):
    playwright_api = pytest.importorskip("playwright.async_api")
    pw = await playwright_api.async_playwright().start()
    try:
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    except Exception as exc:  # noqa: BLE001
        await pw.stop()
        pytest.skip(f"서버 Playwright Chromium 기동 실패 — 실제 캡처 미검증: {str(exc)[:80]}")
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(safe_site)
        use_page(page)
        yield page
    finally:
        await browser.close()
        await pw.stop()


class TestRealServerPlaywright:
    @pytest.mark.asyncio
    async def test_real_screenshot_is_a_reference_to_the_same_page(self, real_page, artifact_dir):
        out = await cct.tool_browser_screenshot(tenant_id="tenant-1")
        assert out.startswith("[스크린샷 참조 — 접근 통제 아티팩트]")
        assert "token=SECRET" not in out and "frag" not in out and "/safe" in out
        assert "iVBOR" not in out
        artifact_id = re.search(r"/artifacts/([\w-]+)", out).group(1)
        png, meta = artifacts.load_screenshot_artifact(artifact_id=artifact_id, tenant_id="tenant-1")
        assert png.startswith(b"\x89PNG") and len(png) > 1000
        assert meta["source"] == "server_playwright" and meta["url"].endswith("/safe")
        assert "/safe?token=SECRET" in real_page.url  # 새로 탐색하지 않았다(URL 유지)

    @pytest.mark.asyncio
    async def test_real_snapshot_never_exposes_password_value(self, real_page):
        out = await cct.tool_browser_snapshot()
        assert "S3cr3t-Value" not in out
        assert "저장" in out

    @pytest.mark.asyncio
    async def test_verified_click_with_expectation(self, real_page):
        out = await cct.tool_browser_click("#save", expect={"text_present": "SAVED"})
        assert out.startswith("[클릭 완료]")

    @pytest.mark.asyncio
    async def test_noop_click_is_not_reported_complete(self, real_page):
        out = await cct.tool_browser_click("#noop")
        assert "[클릭 전달됨·변화 없음 — 미검증]" in out and "[클릭 완료]" not in out

    @pytest.mark.asyncio
    async def test_wrong_expectation_is_not_verified(self, real_page):
        out = await cct.tool_browser_click("#save", expect={"text_present": "NEVER"})
        assert "CLICK_NOT_VERIFIED" in out and "[클릭 완료]" not in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("selector,code", [
        ("text=삭제 안내문", "ELEMENT_NOT_ACTIONABLE"),
        ("#missing", "ELEMENT_NOT_FOUND"),
        ("button.dup", "SELECTOR_AMBIGUOUS"),
        ("#dis", "ELEMENT_DISABLED"),
        ("#hid", "ELEMENT_HIDDEN"),
        ("#covered", "ELEMENT_OBSCURED"),
    ])
    async def test_strict_selector_codes(self, real_page, selector, code):
        out = await cct.tool_browser_click(selector)
        assert out.startswith("[ERROR]") and code in out, out

    @pytest.mark.asyncio
    async def test_text_selector_clicks_button_not_card(self, real_page):
        out = await cct.tool_browser_click("text=확정", expect={"text_present": "CONFIRMED"})
        assert out.startswith("[클릭 완료]")

    @pytest.mark.asyncio
    async def test_nth_disambiguates_duplicates(self, real_page):
        out = await cct.tool_browser_click("button.dup", nth=1)
        assert out.startswith("[클릭 전달됨")
        out = await cct.tool_browser_click("button.dup", nth=5)
        assert "NTH_OUT_OF_RANGE" in out

    @pytest.mark.asyncio
    async def test_checkbox_checked_expectation(self, real_page):
        out = await cct.tool_browser_click("#agree", expect={"checked": {"selector": "#agree", "value": True}})
        assert out.startswith("[클릭 완료]")


# ── 읽기 엔드포인트 ─────────────────────────────────────────────────────

class TestArtifactReadEndpoint:
    @staticmethod
    def _ctx(tenant: str) -> dict:
        return {"tenant": {"id": tenant}, "membership": {"user_id": "u1"}}

    @pytest.mark.asyncio
    async def test_same_tenant_gets_png_with_no_store_headers(self, artifact_dir):
        from app.api import browser_tasks

        ref = _save()
        resp = await browser_tasks.api_get_browser_artifact(
            ref["artifact_id"], session_id="sess-1", work_key="wk-a", context=self._ctx("tenant-1"),
        )
        assert resp.body == PNG and resp.media_type == "image/png"
        assert resp.headers["cache-control"] == "private, no-store"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tenant,session_id,work_key", [
        ("tenant-2", "sess-1", "wk-a"),
        ("tenant-1", "sess-X", "wk-a"),
        ("tenant-1", "sess-1", "wk-X"),
    ])
    async def test_every_denial_is_the_same_404(self, artifact_dir, tenant, session_id, work_key):
        from fastapi import HTTPException

        from app.api import browser_tasks

        ref = _save()
        with pytest.raises(HTTPException) as err:
            await browser_tasks.api_get_browser_artifact(
                ref["artifact_id"], session_id=session_id, work_key=work_key, context=self._ctx(tenant),
            )
        assert err.value.status_code == 404 and err.value.detail == "browser_artifact_not_found"

    @pytest.mark.asyncio
    async def test_unknown_id_is_indistinguishable_from_denied(self, artifact_dir):
        from fastapi import HTTPException

        from app.api import browser_tasks

        with pytest.raises(HTTPException) as err:
            await browser_tasks.api_get_browser_artifact("A" * 32, session_id="", work_key="", context=self._ctx("tenant-1"))
        assert err.value.status_code == 404 and err.value.detail == "browser_artifact_not_found"
