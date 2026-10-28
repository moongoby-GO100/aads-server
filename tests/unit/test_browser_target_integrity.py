"""PC 브라우저 동일 탭 보장 + 클릭 관측 신뢰성 (AADS-SB-PC-INTEGRITY-20261028).

- 순수 단위: 명시 target 의 NOT_FOUND/MISMATCH 와 파생 target 의 비엄격 선택.
- 실제 Chromium(CDP) 통합: 안전한 로컬 HTML 로 두 탭·두 work_key 교차, strict selector,
  no-op 클릭, 후조건 검증. Chromium 을 띄울 수 없으면 해당 테스트만 skip 된다(미검증으로 보고).
"""
from __future__ import annotations

import glob
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pc_agent"))

from commands import browser_auto  # noqa: E402
from commands.browser_auto import (  # noqa: E402
    CDPCommandError,
    CDPSessionManager,
    _select_page_targets,
    browser_click,
    browser_eval,
)


def _targets(*items):
    return [
        {"id": tid, "type": "page", "url": url, "title": "", "webSocketDebuggerUrl": f"ws://x/{tid}"}
        for tid, url in items
    ]


@pytest.fixture(autouse=True)
def _clean_sessions():
    CDPSessionManager._sessions.clear()
    yield
    CDPSessionManager._sessions.clear()


class TestExplicitTargetSelection:
    @pytest.mark.asyncio
    async def test_explicit_target_missing_is_not_found_without_fallback(self, monkeypatch):
        async def fake_list(port):
            return _targets(("A", "https://a.example/x"), ("B", "https://b.example/y"))

        monkeypatch.setattr(browser_auto, "_list_cdp_targets", fake_list)
        with pytest.raises(CDPCommandError) as err:
            await _select_page_targets(9222, target_id="GONE", strict_target=True)
        assert err.value.code == "TARGET_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_explicit_target_wrong_domain_is_mismatch(self, monkeypatch):
        async def fake_list(port):
            return _targets(("A", "https://a.example/x"), ("B", "https://b.example/y"))

        monkeypatch.setattr(browser_auto, "_list_cdp_targets", fake_list)
        with pytest.raises(CDPCommandError) as err:
            await _select_page_targets(
                9222, target_id="A", strict_target=True, expected_url="https://b.example/"
            )
        assert err.value.code == "TARGET_MISMATCH"

    @pytest.mark.asyncio
    async def test_explicit_target_returns_only_that_tab(self, monkeypatch):
        async def fake_list(port):
            return _targets(("A", "https://a.example/x"), ("B", "https://b.example/y"))

        monkeypatch.setattr(browser_auto, "_list_cdp_targets", fake_list)
        pages = await _select_page_targets(
            9222, target_id="B", strict_target=True, expected_url="https://b.example/"
        )
        assert [p["targetId"] for p in pages] == ["B"]

    @pytest.mark.asyncio
    async def test_derived_target_remains_non_strict(self, monkeypatch):
        """세션에서 파생된 target 은 닫혀도 다른 탭으로 복구할 수 있어야 한다(기존 동작 유지)."""
        async def fake_list(port):
            return _targets(("A", "https://a.example/x"))

        monkeypatch.setattr(browser_auto, "_list_cdp_targets", fake_list)
        pages = await _select_page_targets(9222, target_id="STALE", strict_target=False)
        assert [p["targetId"] for p in pages] == ["A"]

    @pytest.mark.asyncio
    async def test_click_with_missing_explicit_target_never_dispatches(self, monkeypatch):
        async def fake_list(port):
            return _targets(("A", "https://a.example/x"))

        async def fake_probe(port):
            return {"Browser": "fake", "webSocketDebuggerUrl": "ws://x/browser"}

        monkeypatch.setattr(browser_auto, "_list_cdp_targets", fake_list)
        monkeypatch.setattr(browser_auto, "_probe_cdp_version", fake_probe)
        resp = await browser_click({"selector": "#save", "port": 9222, "target_id": "GONE"})
        assert resp["status"] == "error"
        assert resp["data"]["error_code"] == "TARGET_NOT_FOUND"
        assert resp["data"].get("dispatch_state") == "not_dispatched"
        assert resp["data"].get("retry_safe") is False
        assert not resp["data"].get("dispatched")


class TestExpectationValidation:
    @pytest.mark.asyncio
    async def test_unknown_expect_key_is_rejected_before_any_cdp_call(self):
        resp = await browser_click({"selector": "#x", "expect": {"nonsense": 1}})
        assert resp["status"] == "error"
        assert resp["data"]["error_code"] == "INVALID_EXPECT"
        assert resp["data"]["dispatched"] is False

    @pytest.mark.asyncio
    async def test_invalid_nth_is_rejected(self):
        resp = await browser_click({"selector": "#x", "nth": "abc"})
        assert resp["data"]["error_code"] == "INVALID_NTH"


# ── 실제 Chromium 통합 ────────────────────────────────────────────────

_PAGE_A = """<!doctype html><html><head><meta charset="utf-8"><title>A</title></head><body>
<h1 id="who">PAGE-A</h1>
<button id="save" onclick="document.getElementById('who').textContent='A-SAVED'">저장</button>
<button id="noop">아무일없음</button>
<button id="dis" disabled>비활성</button>
<button class="dup">중복</button><button class="dup">중복</button>
<button id="hid" style="display:none">숨김</button>
<div id="wrapper" style="padding:8px">삭제 안내문</div>
<div class="card"><button id="confirm" onclick="document.getElementById('who').textContent='A-CONFIRMED'">확정</button></div>
<label><input type="checkbox" id="agree"> 동의</label>
<div style="position:relative;height:40px"><button id="covered" style="position:absolute;left:0;top:0;width:120px;height:30px">가려짐</button>
<div id="shield" style="position:absolute;left:0;top:0;width:120px;height:30px;background:#ccc"></div></div>
<button id="go" onclick="history.pushState({}, '', '/a-next')">이동</button>
</body></html>"""

_PAGE_B = """<!doctype html><html><head><meta charset="utf-8"><title>B</title></head><body>
<h1 id="who">PAGE-B</h1>
<button id="save" onclick="document.getElementById('who').textContent='B-SAVED'">저장</button>
<label><input type="checkbox" id="agree"> 동의</label>
</body></html>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = (_PAGE_B if self.path.startswith("/b") else _PAGE_A).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _find_chromium() -> str:
    for pattern in (
        "/root/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
        "/root/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
    ):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return shutil.which("chromium") or shutil.which("google-chrome") or ""


@pytest.fixture(scope="module")
def chromium_cdp():
    exe = _find_chromium()
    if not exe:
        pytest.skip("Chromium 실행 파일 없음 — 실제 CDP 통합 미검증")
    web_port = _free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", web_port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cdp_port = _free_port()
    profile = tempfile.mkdtemp(prefix="aads-it-profile-")
    proc = subprocess.Popen(
        [exe, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         f"--remote-debugging-port={cdp_port}", f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=1).read()
                ready = True
                break
            except Exception:
                time.sleep(0.3)
        if not ready:
            pytest.skip("Chromium CDP 기동 실패 — 실제 CDP 통합 미검증")

        def new_tab(path: str) -> str:
            req = urllib.request.Request(
                f"http://127.0.0.1:{cdp_port}/json/new?http://127.0.0.1:{web_port}{path}", method="PUT"
            )
            return json.loads(urllib.request.urlopen(req, timeout=5).read())["id"]

        tab_a = new_tab("/a")
        tab_b = new_tab("/b")
        time.sleep(1.0)
        yield {"port": cdp_port, "web": web_port, "tab_a": tab_a, "tab_b": tab_b}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)


async def _text(env, tab, selector="#who"):
    resp = await browser_eval({
        "port": env["port"], "target_id": tab,
        "expression": f"document.querySelector({json.dumps(selector)}).textContent",
    })
    assert resp["status"] == "success", resp
    return str(resp["data"].get("value", ""))


class TestRealChromiumTabIntegrity:
    @pytest.mark.asyncio
    async def test_two_tabs_two_work_keys_cross_match(self, chromium_cdp):
        env = chromium_cdp
        port, a, b = env["port"], env["tab_a"], env["tab_b"]
        a_url = f"http://127.0.0.1:{env['web']}/a"
        ra = await browser_click({
            "port": port, "work_key": "wk-a", "target_id": a, "expected_url": a_url,
            "selector": "#save", "expect": {"text_present": "A-SAVED"},
        })
        assert ra["status"] == "success", ra
        assert ra["data"]["target_id"] == a
        assert ra["data"]["verified"] is True
        assert ra["data"]["verification_state"] == "verified"
        assert "/a" in ra["data"]["target_url"]

        rb = await browser_click({
            "port": port, "work_key": "wk-b", "target_id": b,
            "selector": "#save", "expect": {"text_present": "B-SAVED"},
        })
        assert rb["status"] == "success", rb
        assert rb["data"]["target_id"] == b

        # 서로의 탭이 오염되지 않았는지 교차 확인(target + URL + 텍스트).
        assert "A-SAVED" in str(await _text(chromium_cdp, a))
        assert "B-SAVED" in str(await _text(chromium_cdp, b))

    @pytest.mark.asyncio
    async def test_expected_text_of_other_tab_is_not_verified(self, chromium_cdp):
        """A 탭을 클릭하고 B 의 문구를 기대하면 검증 실패여야 한다."""
        env = chromium_cdp
        resp = await browser_click({
            "port": env["port"], "target_id": env["tab_a"], "selector": "#save",
            "expect": {"text_present": "B-SAVED"}, "expect_timeout_seconds": 1,
        })
        assert resp["status"] == "error"
        assert resp["data"]["error_code"] == "CLICK_NOT_VERIFIED"
        assert resp["data"]["dispatched"] is True
        assert resp["data"]["verified"] is False
        assert resp["data"]["retry_safe"] is False

    @pytest.mark.asyncio
    async def test_target_with_wrong_expected_host_is_rejected(self, chromium_cdp):
        env = chromium_cdp
        before = await _text(chromium_cdp, env["tab_b"])
        resp = await browser_click({
            "port": env["port"], "target_id": env["tab_b"],
            "expected_url": "https://other.example/", "selector": "#save",
        })
        assert resp["status"] == "error"
        assert resp["data"]["error_code"] == "TARGET_MISMATCH"
        assert not resp["data"].get("dispatched")
        assert await _text(chromium_cdp, env["tab_b"]) == before

    @pytest.mark.asyncio
    async def test_closed_explicit_target_does_not_fall_back_to_other_tab(self, chromium_cdp):
        env = chromium_cdp
        resp = await browser_click({
            "port": env["port"], "target_id": "NO-SUCH-TAB", "selector": "#save",
        })
        assert resp["status"] == "error"
        assert resp["data"]["error_code"] == "TARGET_NOT_FOUND"
        assert not resp["data"].get("dispatched")


class TestRealChromiumClickObservation:
    @pytest.mark.asyncio
    async def test_noop_click_is_dispatched_but_not_verified(self, chromium_cdp):
        env = chromium_cdp
        resp = await browser_click({
            "port": env["port"], "target_id": env["tab_a"], "selector": "#noop",
            "verify_settle_ms": 200,
        })
        data = resp["data"]
        assert resp["status"] == "success"
        assert data["dispatched"] is True
        assert data["verified"] is False
        assert data["verification_state"] == "no_observed_effect"
        assert data["retry_safe"] is False

    @pytest.mark.asyncio
    async def test_click_with_observed_change_is_not_marked_verified(self, chromium_cdp):
        env = chromium_cdp
        resp = await browser_click({
            "port": env["port"], "target_id": env["tab_a"], "selector": "#confirm",
            "verify_settle_ms": 200,
        })
        data = resp["data"]
        assert data["dispatched"] is True
        assert data["verified"] is False  # 기대 후조건 없이 변화만 관측된 경우는 verified 가 아니다
        assert data["verification_state"] == "observed_change"

    @pytest.mark.asyncio
    async def test_url_change_expectation_verified_against_real_href(self, chromium_cdp):
        env = chromium_cdp
        resp = await browser_click({
            "port": env["port"], "target_id": env["tab_a"], "selector": "#go",
            "expect": {"url_contains": "/a-next"},
        })
        assert resp["status"] == "success", resp
        assert resp["data"]["verified"] is True
        assert resp["data"]["href_after"].endswith("/a-next")
        assert resp["data"]["target_url"].endswith("/a-next")

    @pytest.mark.asyncio
    async def test_checkbox_checked_expectation(self, chromium_cdp):
        env = chromium_cdp
        resp = await browser_click({
            "port": env["port"], "target_id": env["tab_b"], "selector": "#agree",
            "expect": {"checked": {"selector": "#agree", "value": True}},
        })
        assert resp["status"] == "success", resp
        assert resp["data"]["verified"] is True
        assert resp["data"]["checked_before"] is False
        assert resp["data"]["checked_after"] is True


class TestRealChromiumStrictSelector:
    async def _click(self, env, selector, **extra):
        return await browser_click({
            "port": env["port"], "target_id": env["tab_a"], "selector": selector,
            "verify_settle_ms": 100, **extra,
        })

    @pytest.mark.asyncio
    async def test_text_selector_does_not_match_wrapper_div(self, chromium_cdp):
        resp = await self._click(chromium_cdp, "text=삭제 안내문")
        assert resp["status"] == "error"
        assert resp["data"]["error_code"] == "ELEMENT_NOT_ACTIONABLE"
        assert resp["data"]["dispatched"] is False

    @pytest.mark.asyncio
    async def test_text_selector_picks_button_inside_card_not_card(self, chromium_cdp):
        resp = await self._click(chromium_cdp, "text=확정")
        assert resp["status"] == "success", resp
        assert resp["data"]["target"]["tag"] == "button"

    @pytest.mark.asyncio
    async def test_not_found(self, chromium_cdp):
        resp = await self._click(chromium_cdp, "#does-not-exist")
        assert resp["data"]["error_code"] == "ELEMENT_NOT_FOUND"
        assert resp["data"]["matched_count"] == 0

    @pytest.mark.asyncio
    async def test_many_is_ambiguous_and_nth_selects(self, chromium_cdp):
        resp = await self._click(chromium_cdp, "button.dup")
        assert resp["data"]["error_code"] == "SELECTOR_AMBIGUOUS"
        assert resp["data"]["visible_count"] == 2
        assert resp["data"]["dispatched"] is False
        ok = await self._click(chromium_cdp, "button.dup", nth=1)
        assert ok["status"] == "success"
        out = await self._click(chromium_cdp, "button.dup", nth=5)
        assert out["data"]["error_code"] == "NTH_OUT_OF_RANGE"

    @pytest.mark.asyncio
    async def test_disabled(self, chromium_cdp):
        resp = await self._click(chromium_cdp, "#dis")
        assert resp["data"]["error_code"] == "ELEMENT_DISABLED"
        assert resp["data"]["dispatched"] is False

    @pytest.mark.asyncio
    async def test_hidden(self, chromium_cdp):
        resp = await self._click(chromium_cdp, "#hid")
        assert resp["data"]["error_code"] == "ELEMENT_HIDDEN"

    @pytest.mark.asyncio
    async def test_obscured(self, chromium_cdp):
        resp = await self._click(chromium_cdp, "#covered")
        assert resp["data"]["error_code"] == "ELEMENT_OBSCURED"
        assert resp["data"]["dispatched"] is False
