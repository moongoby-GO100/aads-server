"""서버 _LocalAgentPage: 같은 탭 고정 / 클릭 재전송 금지 / 관측 재시도 / 실제 URL 반영 (AADS-SB-PC-INTEGRITY-20261028)."""
from __future__ import annotations

import json

import pytest

from app.browser_bridge import service as bridge_service
from app.browser_bridge.models import BrowserBridgeSession, BrowserEndpoint, BrowserEndpointKind, utcnow
from app.browser_bridge.service import BrowserBridgeError, BrowserBridgeService, _LocalAgentPage
from app.services.pc_agent_manager import pc_agent_manager


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AADS_SERVICE_ROLE", "AADS_PC_AGENT_ROUTE_ACTIVE_API_FIRST", "PC_AGENT_DEFAULT_AGENT_ID",
        "PC_AGENT_DEFAULT_HOSTNAME", "PC_AGENT_DEFAULT_BROWSER_WORK_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(bridge_service, "LOCAL_AGENT_OBSERVE_RETRY_SECONDS", 0.0)


def _page(work_key: str = "wk-a", **metadata) -> tuple[_LocalAgentPage, BrowserBridgeSession, BrowserBridgeService]:
    meta = {"agent_id": "pc-1", "port": "9222", **metadata}
    session = BrowserBridgeSession(
        session_id=f"sess-{work_key}",
        label="t",
        endpoint=BrowserEndpoint(kind=BrowserEndpointKind.LOCAL_AGENT, metadata=meta),
        registered_at=utcnow(),
        work_key=work_key,
    )
    service = BrowserBridgeService()

    async def no_active_route(**_kw):
        return None

    service._execute_pc_agent_route_via_active_api = no_active_route  # type: ignore[method-assign]
    return _LocalAgentPage(session, service), session, service


def _install(monkeypatch, replies):
    """replies: 호출 순서대로 소비되는 result dict 목록(마지막은 반복)."""
    sent: list[tuple[str, dict]] = []

    async def fake(*, command_type, params, **_kw):
        sent.append((command_type, dict(params)))
        idx = min(len(sent) - 1, len(replies) - 1)
        reply = replies[idx]
        return reply(command_type, params) if callable(reply) else reply

    monkeypatch.setattr(pc_agent_manager, "execute_routed_command", fake)
    return sent


def _ok(**data):
    return {"status": "success", "result": {"status": "success", "data": data}}


def _err(code, message="boom", **data):
    return {"status": "error", "error_code": code, "message": message,
            "result": {"status": "error", "data": {"error": message, "error_code": code, **data}}}


class TestTargetPinning:
    @pytest.mark.asyncio
    async def test_target_is_pinned_and_sent_with_next_command(self, monkeypatch):
        page, session, _ = _page()
        sent = _install(monkeypatch, [_ok(value="x", target_id="TAB-A"), _ok(value="y", target_id="TAB-A")])
        await page.evaluate("1")
        await page.evaluate("2")
        assert sent[1][1].get("target_id") == "TAB-A"
        assert session.endpoint.metadata["page_target_id"] == "TAB-A"
        assert sent[0][1].get("work_key") == "wk-a"

    @pytest.mark.asyncio
    async def test_answer_from_another_tab_is_target_mismatch(self, monkeypatch):
        page, _, _ = _page(page_target_id="TAB-A")
        _install(monkeypatch, [_ok(value="x", target_id="TAB-OTHER")])
        with pytest.raises(BrowserBridgeError) as err:
            await page.evaluate("1")
        assert err.value.error_code == "TARGET_MISMATCH"
        assert err.value.detail["retry_safe"] is False
        assert page._target_id == "TAB-A"  # 다른 탭으로 조용히 옮겨 가지 않는다

    @pytest.mark.asyncio
    async def test_two_pages_two_work_keys_keep_their_own_target(self, monkeypatch):
        page_a, sess_a, _ = _page("wk-a", page_target_id="TAB-A")
        page_b, sess_b, _ = _page("wk-b", page_target_id="TAB-B")
        sent = _install(monkeypatch, [lambda c, p: _ok(value="v", target_id=p.get("target_id"))])
        await page_a.evaluate("1")
        await page_b.evaluate("1")
        assert [p.get("target_id") for _, p in sent] == ["TAB-A", "TAB-B"]
        assert [p.get("work_key") for _, p in sent] == ["wk-a", "wk-b"]
        assert sess_a.endpoint.metadata["page_target_id"] == "TAB-A"
        assert sess_b.endpoint.metadata["page_target_id"] == "TAB-B"


class TestClickIsNeverReplayed:
    @pytest.mark.asyncio
    async def test_recoverable_click_failure_does_not_trigger_recovery_replay(self, monkeypatch):
        page, _, service = _page()
        recoveries: list[str] = []

        async def fake_recover(*_a, **_kw):
            recoveries.append("called")
            return None

        service._recover_local_agent_session = fake_recover  # type: ignore[method-assign]
        sent = _install(monkeypatch, [_err("COMMAND_TIMEOUT", "timeout")])
        with pytest.raises(BrowserBridgeError) as err:
            await page.click_detailed("#pay")
        assert len(sent) == 1
        assert recoveries == []
        assert err.value.detail["retry_safe"] is False
        assert err.value.detail["dispatch_state"] == "uncertain"

    @pytest.mark.asyncio
    async def test_read_command_still_recovers_once(self, monkeypatch):
        page, session, service = _page()
        recoveries: list[str] = []

        async def fake_recover(sess, **_kw):
            recoveries.append("called")
            return sess

        service._recover_local_agent_session = fake_recover  # type: ignore[method-assign]
        sent = _install(monkeypatch, [_err("STALE_TARGET"), _ok(value="ok", target_id="T")])
        await page.evaluate("1")
        assert len(sent) == 2 and recoveries == ["called"]

    @pytest.mark.asyncio
    async def test_click_not_verified_surfaces_as_error_with_dispatched_true(self, monkeypatch):
        page, _, _ = _page()
        sent = _install(monkeypatch, [_ok(
            error="클릭은 전달됐지만 기대한 후조건을 확인하지 못했습니다", error_code="CLICK_NOT_VERIFIED",
            dispatched=True, verified=False, retry_safe=False,
        )])
        with pytest.raises(BrowserBridgeError) as err:
            await page.click_detailed("#save", expect={"text_present": "저장됨"})
        assert err.value.error_code == "CLICK_NOT_VERIFIED"
        assert err.value.detail["dispatched"] is True
        assert len(sent) == 1
        assert sent[0][1]["expect"] == {"text_present": "저장됨"}

    @pytest.mark.asyncio
    async def test_legacy_click_stays_non_strict_for_recipes(self, monkeypatch):
        page, _, _ = _page()
        sent = _install(monkeypatch, [_ok(dispatched=True, target_id="T")])
        await page.click("#a")
        assert sent[0][1]["strict"] is False
        sent.clear()
        await page.click_detailed("#a")
        assert sent[0][1]["strict"] is True


class TestObserve:
    def _obs(self, **kw):
        base = {"href": "https://shop.example/cart", "title": "t", "ready": "complete", "text": "본문", "controls": []}
        base.update(kw)
        return json.dumps(base)

    @pytest.mark.asyncio
    async def test_retries_after_document_replacement_then_succeeds(self, monkeypatch):
        page, session, _ = _page()
        sent = _install(monkeypatch, [
            _err("RUNTIME_EVALUATE_ERROR", "Execution context was destroyed, most likely because of a navigation"),
            _ok(value=self._obs(), target_id="TAB-A"),
        ])
        obs = await page.observe()
        assert obs["attempts"] == 2
        assert obs["href"] == "https://shop.example/cart"
        assert obs["target_id"] == "TAB-A"
        assert len(sent) == 2

    @pytest.mark.asyncio
    async def test_explicit_failure_after_bounded_attempts(self, monkeypatch):
        page, _, _ = _page()
        sent = _install(monkeypatch, [_err("RUNTIME_EVALUATE_ERROR", "Execution context was destroyed")])
        with pytest.raises(BrowserBridgeError) as err:
            await page.observe(attempts=3)
        assert err.value.error_code == "SNAPSHOT_UNSTABLE"
        assert len(sent) == 3

    @pytest.mark.asyncio
    async def test_target_mismatch_is_not_retried(self, monkeypatch):
        page, _, _ = _page(page_target_id="TAB-A")
        sent = _install(monkeypatch, [_ok(value=self._obs(), target_id="TAB-OTHER")])
        with pytest.raises(BrowserBridgeError) as err:
            await page.observe()
        assert err.value.error_code == "TARGET_MISMATCH"
        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_loading_document_is_retried_and_last_read_flagged_unstable(self, monkeypatch):
        page, _, _ = _page()
        _install(monkeypatch, [_ok(value=self._obs(ready="loading"), target_id="T")])
        obs = await page.observe(attempts=2)
        assert obs["stable"] is False and obs["attempts"] == 2

    @pytest.mark.asyncio
    async def test_session_url_is_updated_with_real_href(self, monkeypatch):
        page, session, _ = _page(last_url="https://shop.example/home")
        _install(monkeypatch, [_ok(value=self._obs(href="https://shop.example/checkout?step=2"), target_id="T")])
        await page.observe()
        assert page.url == "https://shop.example/checkout?step=2"
        assert session.endpoint.metadata["last_url"] == "https://shop.example/checkout?step=2"

    @pytest.mark.asyncio
    async def test_non_http_href_does_not_overwrite_session_url(self, monkeypatch):
        page, session, _ = _page(last_url="https://shop.example/home")
        _install(monkeypatch, [_ok(value=self._obs(href="about:blank"), target_id="T")])
        await page.observe()
        assert session.endpoint.metadata["last_url"] == "https://shop.example/home"


class TestScreenshotTargetReport:
    @pytest.mark.asyncio
    async def test_screenshot_reports_answering_target(self, monkeypatch):
        import base64

        page, _, _ = _page(page_target_id="TAB-A")
        _install(monkeypatch, [_ok(screenshot_base64=base64.b64encode(b"\x89PNG\r\n\x1a\nxx").decode(), target_id="TAB-A")])
        raw = await page.screenshot()
        assert raw.startswith(b"\x89PNG")
        assert page.last_screenshot_target_id == "TAB-A"
