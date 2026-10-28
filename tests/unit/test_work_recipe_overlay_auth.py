"""dismiss_overlays(허용 목록·닫힘 검증·상한)와 로그인 회귀 auth_required(재로그인·재전송 없음)."""
from __future__ import annotations

from typing import Any

import pytest

from app.services.work_recipe import executor as executor_module
from app.services.work_recipe.player import RecipePlayer
from app.services.work_recipe.schema import RecipeStep, parse_recipe

ROOT = "#promo-modal"
CLOSE = ".close-btn"
OVERLAY = {"domain": "shop.example.com", "root": ROOT, "close": CLOSE}


class ModalPage:
    """모달 하나를 모델링한 가짜 페이지. reopen=N 이면 닫아도 N번 다시 뜬다."""

    def __init__(self, *, visible=True, text="오늘의 이벤트 안내", url="https://shop.example.com/home",
                 closes=True, reopen=0, body="주문 목록"):
        self.url = url
        self.visible, self.text, self.closes, self.reopen, self.body = visible, text, closes, reopen, body
        self.clicks: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    async def evaluate(self, expression, argument=None, **kwargs):
        return {"url": self.url, "rootVisible": self.visible, "closeVisible": self.visible,
                "text": self.text if self.visible else ""}

    async def click(self, selector, **kwargs):
        self.clicks.append(selector)
        self.calls.append(("click", {"selector": selector}))
        if self.closes:
            self.visible = False
            if self.reopen > 0:
                self.reopen -= 1
                self.visible = True

    async def goto(self, url, **kwargs):
        self.calls.append(("navigate", {"url": url}))
        self.url = url

    def locator(self, selector):
        page = self

        class Loc:
            async def aria_snapshot(self):
                return page.body

            async def inner_text(self, **kwargs):
                return page.body

        return Loc()

    async def screenshot(self, **kwargs):
        return b"png"


def _executor(monkeypatch, page, **kwargs):
    async def acquire(**_):
        class Ctx:
            pages = [page]

        return Ctx(), None

    async def save(data, *, prefix=""):
        return "https://aads.newtalk.kr/screenshots/x.png"

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    monkeypatch.setattr(executor_module, "save_png", save)
    return executor_module.BrowserRecipeExecutor(**kwargs)


def _step(**extra):
    return {"action": "dismiss_overlays", "risk": "READ", "overlays": [OVERLAY], **extra}


async def test_absent_modal_is_noop_without_click(monkeypatch):
    page = ModalPage(visible=False)
    result = await _executor(monkeypatch, page)(_step())
    assert result["ok"] is True
    assert result["output"]["overlays_status"] == "no_op"
    assert page.clicks == []


async def test_modal_is_closed_and_verified(monkeypatch):
    page = ModalPage()
    result = await _executor(monkeypatch, page)(_step())
    assert result["ok"] is True
    assert result["output"]["overlays_status"] == "dismissed"
    assert result["output"]["dismissed"][0]["verified_closed"] is True
    assert page.clicks == [f"{ROOT} {CLOSE}"]


async def test_modal_that_does_not_close_after_click_is_blocked(monkeypatch):
    page = ModalPage(closes=False)
    result = await _executor(monkeypatch, page)(_step())
    assert result["ok"] is False and result["status"] == "blocked"
    assert result["recovery"]["reason"] == "overlay_not_closed_after_click"
    assert len(page.clicks) == 1


async def test_modal_reappearing_after_each_close_stops_at_cap(monkeypatch):
    class Reopening(ModalPage):
        """probe 순서: 열림(클릭) → 닫힘 확인 → 다시 열림 …"""

        def __init__(self):
            super().__init__()
            self.probes = 0

        async def evaluate(self, expression, argument=None, **kwargs):
            self.probes += 1
            self.visible = self.probes % 2 == 1
            return await super().evaluate(expression, argument, **kwargs)

        async def click(self, selector, **kwargs):
            self.clicks.append(selector)

    page = Reopening()
    result = await _executor(monkeypatch, page)(_step(max_dismiss=2))
    assert result["ok"] is False and result["status"] == "blocked"
    assert result["recovery"]["reason"] == "overlay_reappeared_over_limit"
    assert len(page.clicks) == 2


@pytest.mark.parametrize("text", ["개인정보 수집 및 이용 동의", "결제 확인이 필요합니다", "보안 경고: 인증서 오류", "Cookie consent"])
async def test_consent_payment_security_modals_are_never_dismissed(monkeypatch, text):
    page = ModalPage(text=text)
    result = await _executor(monkeypatch, page)(_step())
    assert result["ok"] is False and result["status"] == "blocked"
    assert result["recovery"]["reason"] == "overlay_sensitive_content"
    assert page.clicks == []


async def test_modal_on_non_allowlisted_domain_is_left_alone(monkeypatch):
    page = ModalPage(url="https://evil.example.org/home")
    result = await _executor(monkeypatch, page)(_step())
    assert result["ok"] is True and result["output"]["overlays_status"] == "no_op"
    assert page.clicks == []


@pytest.mark.parametrize("bad", [
    {"overlays": [{**OVERLAY, "root": "#consent-modal"}]},
    {"overlays": [{**OVERLAY, "close": ".agree, .ok"}]},
    {"overlays": [{**OVERLAY, "domain": "localhost"}]},
    {"overlays": []},
    {"max_dismiss": 99},
    {"risk": "WRITE_EXTERNAL"},
])
def test_overlay_spec_validation_rejects_unsafe_shapes(bad):
    raw = {"action": "dismiss_overlays", "overlays": [OVERLAY], **bad}
    with pytest.raises(ValueError):
        RecipeStep.from_dict(raw, seq=1)


def test_overlay_step_is_read_only_and_auto_approval_excluded():
    from app.services.work_recipe.auto_approve import READ_ONLY_ACTIONS

    assert "dismiss_overlays" not in READ_ONLY_ACTIONS


# ---------------------------------------------------------------- auth_required


LOGIN_TEXT = "로그인 세션이 만료되었습니다. 다시 로그인해 주세요."


def _auth_recipe():
    return parse_recipe({
        "name": "order_send", "domain": "shop.example.com", "version": 1,
        "steps": [
            {"action": "navigate", "url": "https://shop.example.com/orders", "risk": "READ"},
            {"action": "click", "selector": "#send", "url": "https://shop.example.com/orders", "risk": "WRITE_EXTERNAL"},
        ],
    })


async def test_login_regression_stops_with_auth_required_and_zero_sends(monkeypatch):
    page = ModalPage(body=LOGIN_TEXT)
    executor = _executor(monkeypatch, page, approval_verifier=lambda payload, level: True)
    player = RecipePlayer(executor, retries=2, max_risk="WRITE_EXTERNAL")
    result = await player.play(_auth_recipe(), context={"requires_auth": True})

    assert result.status == "blocked"
    assert result.user_state == "auth_required"
    assert "로그인" in result.user_message
    assert [name for name, _ in page.calls if name == "click"] == []
    blocked = next(step for step in result.steps if step.status == "blocked")
    recovery = blocked.recovery
    assert result.steps[-1].status == "skipped"
    assert recovery["reason"] == "auth_required"
    assert recovery["auto_relogin"] is False and recovery["auto_retry"] is False
    assert recovery["recovery_path"] == "vault_credential_scope"


async def test_login_page_is_normal_when_run_does_not_require_auth(monkeypatch):
    page = ModalPage(body=LOGIN_TEXT)
    executor = _executor(monkeypatch, page)
    result = await executor({"action": "navigate", "url": "https://shop.example.com/", "risk": "READ"})
    assert result["ok"] is True


async def test_logging_in_step_itself_is_not_a_regression(monkeypatch):
    page = ModalPage(body=LOGIN_TEXT)
    executor = _executor(monkeypatch, page)
    result = await executor({
        "action": "navigate", "url": "https://shop.example.com/login", "risk": "READ",
        "context": {"requires_auth": True},
    })
    assert result["ok"] is True


async def test_recovery_plan_for_login_expired_is_human_only():
    from app.services.browser_recipe_recovery import recovery_plan

    plan = recovery_plan(failure_class="login_expired", prior_attempts=0)
    assert plan["action"] == "human_gateway" and plan["status"] == "auth_required"
    assert plan["auto_relogin"] is False and plan["auto_retry"] is False
    assert plan["recovery_path"] == "vault_credential_scope"
