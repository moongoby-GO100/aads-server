"""사이트별 팝업 공용 목록(browser_site_overlays): approved 만 읽는 세 소비자와 관리 API.

① 조회(approved 전용·캐시·테넌트) ② 레시피 실행기 자동 참조 ③ 채팅 클릭 가림 처리 ④ 저장 검증·관리 API ⑤ 시드 마이그레이션.
DB·브라우저 없이 가짜 풀/페이지로 검증한다.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from fastapi import HTTPException

from app.api import browser_site_overlays as overlay_api
from app.api import ceo_chat_tools
from app.services.work_recipe import site_overlays
from app.services.work_recipe.executor import OverlayNotDismissible
from app.services.work_recipe.schema import RecipeStep
from app.services.work_recipe.site_overlays import SiteOverlay, SiteOverlayError
from app.services.work_recipe.site_popups import SitePopupRejected, dismiss_site_popups
from tests.unit.test_work_recipe_overlay_auth import CLOSE, ROOT, ModalPage, _executor

ABLY = "https://my.a-bly.com/dashboard"
INTERCEPT = "Locator.click: <div> from <div role=dialog> subtree intercepts pointer events"


def _row(root: str = '[role="dialog"]', *, domain: str = "my.a-bly.com", tenant: str | None = None,
         close: str = "button.close") -> SiteOverlay:
    return SiteOverlay(id=f"id-{root}", tenant_id=tenant, domain=domain, root_selector=root, close_selector=close)


@pytest.fixture
def approved(monkeypatch: pytest.MonkeyPatch):
    """approved 행을 갈아끼운다. rows 를 바꾸고 invalidate_cache() 하면 다음 조회에 반영된다."""
    holder: dict[str, list[SiteOverlay]] = {"rows": [_row()]}

    async def fake_load() -> list[SiteOverlay]:
        return list(holder["rows"])

    site_overlays.invalidate_cache()
    monkeypatch.setattr(site_overlays, "_load_approved", fake_load)
    yield holder
    site_overlays.invalidate_cache()


# ── ① 조회 ───────────────────────────────────────────────────────────────────


class _Record(dict):
    pass


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.sql = rows, []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if "status = 'approved'" in sql:
            return [r for r in self.rows if r["status"] == "approved"]
        return list(self.rows)


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> "_FakePool":
        return self

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _db_row(status: str, root: str, domain: str = "shop.example.com", tenant: str | None = None) -> dict[str, Any]:
    return {"id": f"id-{status}-{root}", "tenant_id": tenant, "domain": domain, "root_selector": root,
            "close_selector": ".close", "label": "", "status": status}


async def test_only_approved_rows_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([
        _db_row("approved", "#ok-modal"),
        _db_row("candidate", "#cand-modal"),
        _db_row("rejected", "#rej-modal"),
        _db_row("disabled", "#dis-modal"),
    ])
    monkeypatch.setattr(site_overlays, "get_pool", lambda: _FakePool(conn))
    site_overlays.invalidate_cache()
    rows = await site_overlays.approved_overlays_for_host("shop.example.com")
    assert [r.root_selector for r in rows] == ["#ok-modal"]
    assert "status = 'approved'" in conn.sql[0]
    site_overlays.invalidate_cache()


async def test_lookup_is_cached_briefly(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _FakeConn([_db_row("approved", "#ok-modal")])
    monkeypatch.setattr(site_overlays, "get_pool", lambda: _FakePool(conn))
    site_overlays.invalidate_cache()
    await site_overlays.approved_overlays_for_host("shop.example.com")
    await site_overlays.approved_overlays_for_host("shop.example.com")
    assert len(conn.sql) == 1
    assert site_overlays.CACHE_TTL_SECONDS <= 60
    site_overlays.invalidate_cache()


async def test_subdomain_matches_but_lookalike_does_not(approved) -> None:
    approved["rows"] = [_row(domain="a-bly.com")]
    assert await site_overlays.approved_overlays_for_host("my.a-bly.com")
    assert not await site_overlays.approved_overlays_for_host("evil-a-bly.com")
    assert not await site_overlays.approved_overlays_for_host("a-bly.com.evil.io")
    assert not await site_overlays.approved_overlays_for_host("")


async def test_tenant_rows_are_scoped_and_global_rows_are_shared(approved) -> None:
    approved["rows"] = [
        _row('[role="dialog"]'),
        _row("#t1-modal", tenant="11111111-1111-1111-1111-111111111111"),
    ]
    anon = await site_overlays.approved_overlays_for_host("my.a-bly.com")
    t1 = await site_overlays.approved_overlays_for_host("my.a-bly.com", "11111111-1111-1111-1111-111111111111")
    other = await site_overlays.approved_overlays_for_host("my.a-bly.com", "22222222-2222-2222-2222-222222222222")
    assert [r.root_selector for r in anon] == ['[role="dialog"]']
    assert {r.root_selector for r in t1} == {'[role="dialog"]', "#t1-modal"}
    assert [r.root_selector for r in other] == ['[role="dialog"]']


async def test_db_failure_means_nothing_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise RuntimeError("DB pool이 초기화되지 않았습니다")

    monkeypatch.setattr(site_overlays, "get_pool", broken)
    site_overlays.invalidate_cache()
    assert await site_overlays.approved_overlays_for_host("my.a-bly.com") == []


async def test_popup_tool_policy_comes_from_db_rows(approved) -> None:
    approved["rows"] = [_row('[role="dialog"]'), _row("#promo-modal")]
    policy = await site_overlays.site_popup_policy_for_host("my.a-bly.com")
    assert policy is not None and policy.popup_selector == '[role="dialog"], #promo-modal'
    approved["rows"] = []
    site_overlays.invalidate_cache()
    assert await site_overlays.site_popup_policy_for_host("my.a-bly.com") is None


async def test_popup_tool_rejects_host_without_approved_row(approved) -> None:
    approved["rows"] = []

    class _Page:
        url = ABLY

        async def evaluate(self, *_a: Any, **_k: Any) -> Any:  # 화면에 손대면 실패
            raise AssertionError("page touched")

    with pytest.raises(SitePopupRejected) as exc:
        await dismiss_site_popups(_Page())
    assert exc.value.code == "site_not_allowlisted"


# ── ② 레시피 실행기 ───────────────────────────────────────────────────────────

SHOP_URL = "https://shop.example.com/home"


async def test_executor_uses_approved_list_when_step_has_no_overlays(monkeypatch, approved) -> None:
    approved["rows"] = [_row(ROOT, domain="shop.example.com", close=CLOSE)]
    page = ModalPage(url=SHOP_URL)
    result = await _executor(monkeypatch, page)({"action": "dismiss_overlays", "risk": "READ"})
    assert result["ok"] is True and result["output"]["overlays_status"] == "dismissed"
    assert page.clicks == [f"{ROOT} {CLOSE}"]


async def test_executor_ignores_unapproved_rows(monkeypatch, approved) -> None:
    approved["rows"] = []  # candidate/rejected/disabled 는 approved 조회에 나오지 않는다
    page = ModalPage(url=SHOP_URL)
    result = await _executor(monkeypatch, page)({"action": "dismiss_overlays", "risk": "READ"})
    assert result["ok"] is True and result["output"]["overlays_status"] == "no_op"
    assert result["output"]["reason"] == "no_allowlist"
    assert page.clicks == []


async def test_step_overlays_take_priority_over_catalog(monkeypatch, approved) -> None:
    approved["rows"] = [_row("#catalog-modal", domain="shop.example.com", close=".catalog-close")]
    page = ModalPage(url=SHOP_URL)
    step = {"action": "dismiss_overlays", "risk": "READ",
            "overlays": [{"domain": "shop.example.com", "root": ROOT, "close": CLOSE}]}
    result = await _executor(monkeypatch, page)(step)
    assert result["ok"] is True
    assert page.clicks == [f"{ROOT} {CLOSE}"]


async def test_catalog_modal_with_sensitive_text_is_still_refused(monkeypatch, approved) -> None:
    approved["rows"] = [_row(ROOT, domain="shop.example.com", close=CLOSE)]
    page = ModalPage(url=SHOP_URL, text="결제 확인이 필요합니다")
    result = await _executor(monkeypatch, page)({"action": "dismiss_overlays", "risk": "READ"})
    assert result["ok"] is False and result["recovery"]["reason"] == "overlay_sensitive_content"
    assert page.clicks == []


async def test_catalog_modal_that_does_not_close_is_blocked_and_capped(monkeypatch, approved) -> None:
    approved["rows"] = [_row(ROOT, domain="shop.example.com", close=CLOSE)]
    page = ModalPage(url=SHOP_URL, closes=False)
    result = await _executor(monkeypatch, page)({"action": "dismiss_overlays", "risk": "READ"})
    assert result["ok"] is False and result["recovery"]["reason"] == "overlay_not_closed_after_click"
    assert len(page.clicks) == 1


async def test_catalog_host_mismatch_leaves_page_alone(monkeypatch, approved) -> None:
    approved["rows"] = [_row(ROOT, domain="shop.example.com", close=CLOSE)]
    page = ModalPage(url="https://evil.example.org/home")
    result = await _executor(monkeypatch, page)({"action": "dismiss_overlays", "risk": "READ"})
    assert result["output"]["overlays_status"] == "no_op" and page.clicks == []


def test_step_without_overlays_parses_but_empty_or_bad_overlays_still_fail() -> None:
    step = RecipeStep.from_dict({"action": "dismiss_overlays", "risk": "READ"}, seq=1)
    assert "overlays" not in step.to_dict()
    assert RecipeStep.from_dict(step.to_dict(), seq=1).action == "dismiss_overlays"
    with pytest.raises(ValueError):
        RecipeStep.from_dict({"action": "dismiss_overlays", "overlays": []}, seq=1)
    with pytest.raises(ValueError):
        RecipeStep.from_dict({"action": "dismiss_overlays", "max_dismiss": 99}, seq=1)
    with pytest.raises(ValueError):
        RecipeStep.from_dict({"action": "dismiss_overlays", "risk": "WRITE_EXTERNAL"}, seq=1)


# ── ③ 채팅 browser_click 가림 처리 ─────────────────────────────────────────────


class _FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    async def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))


class _FakeTarget:
    """click: 시나리오 리스트 — 'ok' 또는 'obscured'(가림 예외). evaluate: 가림 진단 응답."""

    def __init__(self, page: "_FakeClickPage", clicks: list[str], probe: dict[str, Any]) -> None:
        self.page, self.script, self.probe = page, list(clicks), probe
        self.click_count = 0
        self.probe_args: list[dict[str, Any]] = []

    async def is_disabled(self) -> bool:
        return False

    async def click(self, **_kw: Any) -> None:
        self.click_count += 1
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "obscured":
            raise RuntimeError(INTERCEPT)

    async def evaluate(self, _script: str, args: dict[str, Any]) -> Any:
        self.probe_args.append(args)
        return self.probe


class _FakeLocator:
    def __init__(self, target: _FakeTarget) -> None:
        self.target = target

    async def count(self) -> int:
        return 1

    def nth(self, _i: int) -> Any:
        target = self.target

        class _Item:
            async def is_visible(self) -> bool:
                return True

            is_disabled = target.is_disabled
            click = target.click
            evaluate = target.evaluate

        return _Item()


class _FakeClickPage:
    def __init__(self, *, url: str = ABLY, clicks: list[str] | None = None, probe: dict[str, Any] | None = None,
                 close: dict[str, Any] | None = None, gone: bool = True) -> None:
        self.url = url
        self.mouse = _FakeMouse()
        self.target = _FakeTarget(self, clicks or ["obscured", "ok"], probe or {})
        self.close = close
        self.gone = gone
        self.modes: list[str] = []

    def locator(self, _selector: str) -> _FakeLocator:
        return _FakeLocator(self.target)

    async def evaluate(self, script: str, args: Any = None) -> Any:
        if not isinstance(args, dict):  # _CLICK_FINGERPRINT_JS
            return {"href": self.url, "textLen": 1, "textHash": 1, "stateHash": 1, "dialogs": 0}
        mode = args.get("mode")
        self.modes.append(str(mode))
        if mode == "close_point":
            return {"close": self.close}
        if mode == "gone":
            return {"gone": self.gone}
        return {}


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site_overlays, "_GONE_POLL_SECONDS", 0.0)

    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(ceo_chat_tools.asyncio, "sleep", no_sleep)


_POPUP_PROBE = {
    "obscured": True, "role": "dialog", "text": "점검 안내입니다 " + "가" * 60, "check_text": "점검 안내입니다",
    "root_selector": '[role="dialog"]', "matched_index": 0,
}
_CLOSE_OK = {"via": "selector", "label": "닫기", "x": 10, "y": 20, "hit": True}


async def _click(page: _FakeClickPage, **kw: Any) -> dict[str, Any]:
    return await ceo_chat_tools._server_click(page, "#logout", nth=None, strict=True, expect={}, **kw)


async def test_obscured_by_approved_popup_closes_it_and_retries_exactly_once(approved) -> None:
    page = _FakeClickPage(probe=dict(_POPUP_PROBE), close=dict(_CLOSE_OK))
    data = await _click(page)
    assert data["dispatched"] is True
    assert page.target.click_count == 2
    assert page.mouse.clicks == [(10, 20)]
    assert data["auto_dismissed"] == [{"domain": "my.a-bly.com", "root": '[role="dialog"]', "verified_closed": True}]
    obscured = data["obscured_by"]
    assert obscured["role"] == "dialog" and obscured["root_selector"] == '[role="dialog"]'
    assert obscured["text"] == ("점검 안내입니다 " + "가" * 60)[:40] and len(obscured["text"]) <= 40
    assert page.target.probe_args[0]["roots"] == ['[role="dialog"]']
    assert page.modes[-1] == "cleanup"


async def test_retry_runs_only_once_even_if_still_obscured(approved) -> None:
    page = _FakeClickPage(clicks=["obscured", "obscured", "ok"], probe=dict(_POPUP_PROBE), close=dict(_CLOSE_OK))
    data = await _click(page)
    assert data["error_code"] == "ELEMENT_OBSCURED" and data["dispatched"] is False
    assert page.target.click_count == 2
    assert data["auto_dismissed"] and data["obscured_by"]
    assert len(page.mouse.clicks) == 1


async def test_unmatched_layer_is_refused_with_obscured_by_and_no_close_click(approved) -> None:
    probe = dict(_POPUP_PROBE, matched_index=-1, root_selector="div.sticky-banner", role="")
    page = _FakeClickPage(probe=probe, close=dict(_CLOSE_OK))
    data = await _click(page)
    assert data["error_code"] == "ELEMENT_OBSCURED" and data["dispatched"] is False
    assert data["obscured_by"]["root_selector"] == "div.sticky-banner"
    assert data["auto_dismiss_skipped"] == "not_in_approved_list"
    assert page.target.click_count == 1 and page.mouse.clicks == []
    assert "close_point" not in page.modes


async def test_no_approved_rows_for_host_means_no_roots_and_refusal(approved) -> None:
    approved["rows"] = []
    page = _FakeClickPage(probe=dict(_POPUP_PROBE, matched_index=-1), close=dict(_CLOSE_OK))
    data = await _click(page)
    assert data["error_code"] == "ELEMENT_OBSCURED"
    assert page.target.probe_args[0]["roots"] == []
    assert page.mouse.clicks == [] and page.target.click_count == 1


async def test_other_domain_rows_are_not_used(approved) -> None:
    approved["rows"] = [_row(domain="other.example.com")]
    page = _FakeClickPage(probe=dict(_POPUP_PROBE, matched_index=-1), close=dict(_CLOSE_OK))
    await _click(page)
    assert page.target.probe_args[0]["roots"] == []


async def test_sensitive_popup_is_never_dismissed(approved) -> None:
    page = _FakeClickPage(probe=dict(_POPUP_PROBE, check_text="결제 동의 안내"), close=dict(_CLOSE_OK))
    data = await _click(page)
    assert data["error_code"] == "ELEMENT_OBSCURED"
    assert data["auto_dismiss_skipped"] == "sensitive_popup"
    assert page.mouse.clicks == [] and page.target.click_count == 1


@pytest.mark.parametrize("label", ["하루 동안 보지 않기", "다시 보지 않기", "확인", "동의", "신청하기"])
async def test_forbidden_close_labels_are_never_pressed(approved, label: str) -> None:
    page = _FakeClickPage(probe=dict(_POPUP_PROBE), close=dict(_CLOSE_OK, label=label))
    data = await _click(page)
    assert data["error_code"] == "ELEMENT_OBSCURED"
    assert data["auto_dismiss_skipped"] == "close_label_forbidden"
    assert page.mouse.clicks == [] and page.target.click_count == 1


async def test_label_fallback_requires_exact_close_label(approved) -> None:
    page = _FakeClickPage(probe=dict(_POPUP_PROBE), close=dict(_CLOSE_OK, via="label", label="닫기 전에 읽어보세요"))
    data = await _click(page)
    assert data["auto_dismiss_skipped"] == "close_label_forbidden" and page.mouse.clicks == []


async def test_obscured_close_control_is_not_clicked(approved) -> None:
    page = _FakeClickPage(probe=dict(_POPUP_PROBE), close=dict(_CLOSE_OK, hit=False))
    data = await _click(page)
    assert data["auto_dismiss_skipped"] == "close_obscured" and page.mouse.clicks == []


async def test_popup_that_stays_open_gets_no_retry(approved) -> None:
    page = _FakeClickPage(probe=dict(_POPUP_PROBE), close=dict(_CLOSE_OK), gone=False)
    data = await _click(page)
    assert data["error_code"] == "ELEMENT_OBSCURED"
    assert data["auto_dismiss_skipped"] == "not_closed_after_click"
    assert page.target.click_count == 1
    assert len(page.mouse.clicks) == 2  # 닫기 확인 실패 시 좌표를 다시 구해 한 번 더 — 그 뒤 중단


async def test_probe_failure_falls_back_to_plain_refusal(approved) -> None:
    page = _FakeClickPage()

    async def boom(_script: str, _args: Any) -> Any:
        raise RuntimeError("evaluate failed")

    page.target.evaluate = boom  # type: ignore[method-assign]
    data = await _click(page)
    assert data["error_code"] == "ELEMENT_OBSCURED" and data["dispatched"] is False
    assert "obscured_by" not in data and page.target.click_count == 1


async def test_non_obscured_error_is_still_uncertain(approved) -> None:
    page = _FakeClickPage(clicks=["ok"])

    async def timeout(**_kw: Any) -> None:
        raise RuntimeError("Timeout 10000ms exceeded")

    page.target.click = timeout  # type: ignore[method-assign]
    data = await _click(page)
    assert data["error_code"] == "CLICK_DISPATCH_UNCERTAIN" and "obscured_by" not in data


def test_click_messages_carry_obscured_by_and_auto_dismissed() -> None:
    detail = {"dispatched": False, "dispatch_state": "not_dispatched",
              "obscured_by": {"role": "dialog", "text": "공지", "root_selector": "div.x"},
              "auto_dismiss_skipped": "not_in_approved_list"}
    text = ceo_chat_tools._format_click_error("#a", "ELEMENT_OBSCURED", "가림", detail)
    assert "ELEMENT_OBSCURED" in text and "obscured_by" in text and "div.x" in text
    ok = ceo_chat_tools._format_click_result("#a", {
        "verification_state": "observed_change", "changed": ["url"], "href_before": "a", "href_after": "a",
        "auto_dismissed": [{"domain": "my.a-bly.com", "root": "x", "verified_closed": True}],
        "obscured_by": {"text": "공지"},
    })
    assert "자동 팝업 닫기" in ok and "auto_dismissed" in ok


# ── ④ 저장 검증·관리 API ───────────────────────────────────────────────────────


def test_validation_reuses_recipe_selector_rules() -> None:
    assert site_overlays.validate_overlay_fields("My.A-Bly.com", '[role="dialog"]', "button.close") == (
        "my.a-bly.com", '[role="dialog"]', "button.close")
    bad = [
        ("my.a-bly.com", "#consent-modal", ".close"),
        ("my.a-bly.com", "#login-modal", ".close"),
        ("my.a-bly.com", ".modal", ".agree, .ok"),
        ("my.a-bly.com", ".modal", "button:text-is(닫기)"),
        ("localhost", ".modal", ".close"),
        ("my.a-bly.com", "", ".close"),
        ("my.a-bly.com", ".modal", "a" * 400),
    ]
    for domain, root, close in bad:
        with pytest.raises(SiteOverlayError):
            site_overlays.validate_overlay_fields(domain, root, close)


def test_evidence_allows_references_only_and_masks_secrets() -> None:
    assert site_overlays.sanitize_evidence(None) == {}
    fake_token = "t" * 22
    clean = site_overlays.sanitize_evidence({
        "screenshot_url": f"https://aads.newtalk.kr/screenshots/a.png?token={fake_token}",
    })
    assert fake_token not in json.dumps(clean)
    with pytest.raises(SiteOverlayError):
        site_overlays.sanitize_evidence({"password": "x"})
    with pytest.raises(SiteOverlayError):
        site_overlays.sanitize_evidence(["x"])


class _WriteConn:
    def __init__(self, existing: bool = False, status: str = "candidate") -> None:
        self.existing, self.status, self.sql, self.args = existing, status, [], []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return 1 if self.existing else None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        self.args.append(args)
        base = {"id": "11111111-1111-1111-1111-111111111111", "tenant_id": None, "domain": "my.a-bly.com",
                "root_selector": ".m", "close_selector": ".c", "label": "", "status": self.status, "source": "manual",
                "evidence": "{}", "approved_by": None, "approved_at": None, "created_at": None, "updated_at": None}
        if sql.lstrip().startswith("UPDATE"):
            base["status"] = args[1]
        return base

    def transaction(self) -> "_WriteConn":
        return self

    async def __aenter__(self) -> "_WriteConn":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def test_candidate_registration_is_always_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _WriteConn()
    monkeypatch.setattr(site_overlays, "get_pool", lambda: _FakePool(conn))  # type: ignore[arg-type]
    row = await site_overlays.add_candidate(
        tenant_id="11111111-1111-1111-1111-111111111111", domain="my.a-bly.com", root_selector=".m",
        close_selector=".c", label="공지", source="recorded", evidence={"note": "n"},
    )
    assert row["status"] == "candidate"
    assert "'candidate'" in conn.sql[0]
    with pytest.raises(SiteOverlayError):  # seed 는 API 로 만들 수 없다
        await site_overlays.add_candidate(tenant_id=None, domain="my.a-bly.com", root_selector=".m",
                                          close_selector=".c", source="seed")


async def test_duplicate_candidate_is_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site_overlays, "get_pool", lambda: _FakePool(_WriteConn(existing=True)))  # type: ignore[arg-type]
    with pytest.raises(SiteOverlayError) as exc:
        await site_overlays.add_candidate(tenant_id=None, domain="my.a-bly.com", root_selector=".m", close_selector=".c")
    assert exc.value.code == "overlay_exists"


@pytest.mark.parametrize("current,decision,ok", [
    ("candidate", "approved", True), ("candidate", "rejected", True), ("candidate", "disabled", False),
    ("approved", "disabled", True), ("approved", "approved", False), ("rejected", "approved", False),
    ("disabled", "approved", True), ("rejected", "disabled", False),
])
async def test_status_transitions(monkeypatch: pytest.MonkeyPatch, current: str, decision: str, ok: bool) -> None:
    monkeypatch.setattr(site_overlays, "get_pool", lambda: _FakePool(_WriteConn(status=current)))  # type: ignore[arg-type]
    call = site_overlays.decide_overlay(
        overlay_id="11111111-1111-1111-1111-111111111111", tenant_id=None, decision=decision, decided_by="a@b.c")
    if ok:
        assert (await call)["status"] == decision
    else:
        with pytest.raises(SiteOverlayError) as exc:
            await call
        assert exc.value.code == "invalid_transition"


async def test_decision_invalidates_cache(monkeypatch: pytest.MonkeyPatch, approved) -> None:
    monkeypatch.setattr(site_overlays, "get_pool", lambda: _FakePool(_WriteConn()))  # type: ignore[arg-type]
    await site_overlays.approved_overlays_for_host("my.a-bly.com")
    assert site_overlays._cache["rows"]
    await site_overlays.decide_overlay(
        overlay_id="11111111-1111-1111-1111-111111111111", tenant_id=None, decision="approved", decided_by="a@b.c")
    assert not site_overlays._cache["rows"]


def _ctx(user_id: str = "9f1c3b7e-0000-4000-8000-000000000001", email: str = "ceo@example.com") -> dict[str, Any]:
    return {
        "tenant": {"id": "11111111-1111-1111-1111-111111111111"},
        "membership": {"id": "m1", "user_id": user_id},
        "user": {"id": user_id, "email": email, "is_internal_admin": True},
    }


async def test_api_decision_requires_human_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(**_kw: Any) -> Any:
        raise AssertionError("must not decide")

    monkeypatch.setattr(site_overlays, "decide_overlay", never)
    service = {
        "tenant": {"id": "11111111-1111-1111-1111-111111111111"},
        "membership": {"id": "internal-pipeline-call", "user_id": "system:pipeline"},
        "user": {"user_id": "system:pipeline", "email": "system@aads.internal", "is_internal_admin": True},
    }
    with pytest.raises(HTTPException) as exc:
        await overlay_api.decide_site_overlay("x", overlay_api.SiteOverlayDecisionIn(status="approved"), service)
    assert exc.value.status_code == 403


async def test_api_decision_uses_authenticated_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_decide(**kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"status": kw["decision"]}

    monkeypatch.setattr(site_overlays, "decide_overlay", fake_decide)
    out = await overlay_api.decide_site_overlay("x", overlay_api.SiteOverlayDecisionIn(status="approved"), _ctx())
    assert out["status"] == "approved" and seen["decided_by"] == "ceo@example.com"


async def test_api_register_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def conflict(**_kw: Any) -> Any:
        raise SiteOverlayError("overlay_exists", "dup")

    monkeypatch.setattr(site_overlays, "add_candidate", conflict)
    body = overlay_api.SiteOverlayCandidateIn(domain="my.a-bly.com", root_selector=".m", close_selector=".c")
    with pytest.raises(HTTPException) as exc:
        await overlay_api.register_site_overlay_candidate(body, _ctx())
    assert exc.value.status_code == 409


def test_api_is_mounted_behind_console_admin() -> None:
    from app.main import app

    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/api/v1/ohvis/site-overlays" in paths
    assert "/api/v1/ohvis/site-overlays/{overlay_id}/decision" in paths
    for route in overlay_api.router.routes:
        deps = [d.call for d in route.dependant.dependencies]  # type: ignore[attr-defined]
        assert overlay_api.require_console_admin in deps


# ── ⑤ 시드 마이그레이션 ────────────────────────────────────────────────────────

_MIGRATION = pathlib.Path(__file__).resolve().parents[2] / "migrations" / "20261028_browser_site_overlays.sql"


def test_migration_seeds_ably_as_approved_without_destructive_statements() -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    upper = sql.upper()
    assert "DROP " not in upper and "TRUNCATE" not in upper and "DELETE FROM" not in upper
    assert "CREATE TABLE IF NOT EXISTS browser_site_overlays" in sql
    assert "'approved', 'seed'" in sql and "'my.a-bly.com'" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    for column in ("tenant_id", "domain", "root_selector", "close_selector", "label", "status", "source",
                   "evidence", "approved_by", "approved_at", "created_at", "updated_at"):
        assert column in sql
    for status in ("candidate", "approved", "rejected", "disabled"):
        assert f"'{status}'" in sql


def test_seed_rows_pass_validation_and_match_static_allowlist() -> None:
    from app.services.work_recipe.schema import SITE_POPUP_ALLOWLIST

    sql = _MIGRATION.read_text(encoding="utf-8")
    roots = [r.strip() for r in SITE_POPUP_ALLOWLIST["my.a-bly.com"].popup_selector.split(",")]
    for root in roots:
        assert f"'{root}'" in sql
        site_overlays.validate_overlay_fields("my.a-bly.com", root, 'button[aria-label="close" i]')


def test_rollback_keeps_the_table() -> None:
    down = (_MIGRATION.parent / "rollback" / "20261028_browser_site_overlays.down.sql").read_text(encoding="utf-8")
    statements = "\n".join(line for line in down.splitlines() if not line.lstrip().startswith("--")).upper()
    assert "DROP" not in statements and "TRUNCATE" not in statements and "DELETE" not in statements
    assert "'DISABLED'" in statements


async def test_modal_not_dismissible_error_type_is_unchanged() -> None:
    assert issubclass(OverlayNotDismissible, RuntimeError)
