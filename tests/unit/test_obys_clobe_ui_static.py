"""오비서 클로브 자료현황·거래검토·경영요약 UI 배선 정적 검증.

브라우저 없이 확인할 수 있는 계약만 본다 — 화면 동작은 별도 Playwright 캡처로 검증한다.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "modules" / "clobe-collection.js").read_text(encoding="utf-8")
CSS = (ROOT / "modules" / "clobe-collection.css").read_text(encoding="utf-8")
SW = (ROOT / "sw.js").read_text(encoding="utf-8")

VIEWS = ["clobeOverview", "clobeReview", "clobeSummary", "clobeSettings"]


def test_views_are_wired_in_index():
    for view in VIEWS:
        assert f'id="{view}View"' in INDEX
        assert f'data-view="{view}"' in INDEX
        assert f'"{view}"' in INDEX.split("const appViewNames = [", 1)[1].split("];", 1)[0]
    assert "window.obysClobe?.open(currentView)" in INDEX


def test_assets_are_versioned_and_loaded():
    assert re.search(r"modules/clobe-collection\.css\?v=\d{8}-r\d+", INDEX)
    assert re.search(r"modules/clobe-collection\.js\?v=\d{8}-r\d+", INDEX)


def test_existing_ledger_views_are_untouched():
    for view in ("salesLedger", "purchaseLedger", "bankLedger", "cardLedger"):
        assert f'id="{view}View"' in INDEX
    assert "window.obysLedgerDetails?.open(currentView)" in INDEX
    assert "modules/ledger-details.js" in INDEX
    assert "modules/store-assistant-v2.js" in INDEX


def test_employee_experience_does_not_get_clobe_views():
    block = INDEX.split("const employeeViewNames", 1)[1].split("];", 1)[0]
    assert not any(view in block for view in VIEWS)


def test_service_worker_version_bumped_and_api_not_cached():
    assert 'CACHE_VERSION = "obys-clock-shell-20261009-r1"' in SW
    assert "obys-collections" not in SW
    assert "/api/" not in SW.split("APP_SHELL", 1)[1].split("]", 1)[0]


def test_module_uses_only_collection_endpoints_and_no_secrets():
    assert "/api/v1/obys-collections" in JS
    assert "/api/v1/integrations/clobe" in JS
    for forbidden in ("ANTHROPIC", "sk-ant", "api_key", "apikey", "mcp_token", "Bearer sk"):
        assert forbidden.lower() not in JS.lower()


def test_module_does_not_leak_internal_identifiers_into_copy():
    visible = re.findall(r'"([^"\n]*[가-힣][^"\n]*)"', JS)
    joined = "\n".join(visible)
    for token in ("run_id", "job_id", "pipeline", "runner-", "lease_", "model_"):
        assert token not in joined


def test_summary_never_adds_cash_card_and_uses_integer_cents():
    assert "Math.round(number * 100)" in JS
    assert "total.cash += 1; return;" in JS
    assert "total.card += 1; return;" in JS


def test_confirmation_flow_guards_confirm_and_mismatch():
    assert "되돌릴 수 없습니다" in JS
    assert "state.ackMismatch" in JS
    assert 'state.pending?.scope === "one"' in JS
    assert 'state.pending?.scope === "bulk"' in JS


def test_every_failure_class_has_recovery_copy():
    for marker in ("reauth_required", "collection_in_progress", "role_not_allowed",
                   "tenant_not_in_collection_scope", "company_not_linked", "network"):
        assert marker in JS
    for kind in ("session", "expired", "network", "none", "nodata", "permission"):
        assert f'data-clobe-failure="{kind}"' in JS or f'kind: "{kind}"' in JS


def test_drawer_is_above_mobile_dock_and_touch_targets_are_44px():
    assert "z-index: 1900" in CSS
    assert "min-height: 44px" in CSS
    assert "max-width: 820px" in CSS
