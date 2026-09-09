"""Static hard gates for the additive store-assistant v2 skin."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "app/static/apps/yeoljeong-finance/index.html"
V2_JS = ROOT / "app/static/apps/yeoljeong-finance/modules/store-assistant-v2.js"
V2_CSS = ROOT / "app/static/apps/yeoljeong-finance/modules/store-assistant-v2.css"


class _Ids(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.ids.update(value for key, value in attrs if key == "id" and value)


def _source() -> tuple[str, str, str]:
    return INDEX.read_text(encoding="utf-8"), V2_JS.read_text(encoding="utf-8"), V2_CSS.read_text(encoding="utf-8")


def test_all_legacy_views_and_critical_dom_ids_remain() -> None:
    html, _, _ = _source()
    parser = _Ids()
    parser.feed(html)
    views = set(re.findall(r'data-view="([^"]+)"', html))
    assert {
        "dashboard", "tasks", "reports", "documents", "sales", "accounts",
        "employees", "inventory", "tax", "approvals", "alerts", "onboarding",
        "contracts", "attendance", "payroll", "business", "integrations",
        "margins", "audit", "entries", "closing", "settings",
    } <= views
    assert {
        "authGate", "appShell", "monthFilter", "branchFilter", "csvBtn",
        "entryForm", "shiftForm", "onboardingUploadForm", "contractForm",
        "payrollForm", "fileInput", "bankAccountSelect", "uploadBankFileBtn", "auditRows",
    } <= parser.ids


def test_legacy_api_and_bank_upload_paths_remain() -> None:
    html, js, _ = _source()
    for marker in (
        "/api/v1/auth", "/api/v1/yeoljeong-finance",
        "/bank-transactions/upload", "/onboarding/documents", "/contracts",
        "/payroll", "/sync",
    ):
        assert marker in html
    for prefix in (
        "/api/v1/yeoljeong-dashboard", "/api/v1/yeoljeong-inventory",
        "/api/v1/yeoljeong-accounting", "/api/v1/yeoljeong-ops",
    ):
        assert prefix in js
    assert "business_id" in js and "branch_id" in js


def test_v2_is_additive_accessible_and_has_no_sample_metrics() -> None:
    html, js, css = _source()
    assert "store-assistant-v2.css" in html and "store-assistant-v2.js" in html
    assert "44px" in css and "v2-bottom-nav" in css and "overflow-x: auto" in css
    assert 'event.key !== "Escape"' in js
    assert 'aria-live' in js and 'aria-label", "모바일 주요 화면"' in js
    assert "마지막 동기화" in js and "재시도" in js
    assert "로그인이 만료되었습니다." in js and "권한이 없습니다." in js
    assert not re.search(r"mock|sampleData|샘플 숫자", js, re.IGNORECASE)


def test_existing_business_workflows_are_still_bound() -> None:
    html, _, _ = _source()
    actions = set(re.findall(r'data-([a-z][a-z0-9-]+)=', html))
    assert {
        "view", "sync-integration", "sync-financial-integration", "open-import",
        "review-join", "review-doc", "preview-contract", "request-contract",
        "sign-contract", "delete-contract", "edit-payroll", "delete-payroll",
    } <= actions
    for function_name in (
        "loadAuthSession", "financeApi", "renderOperationalPages", "setView",
        "uploadBankFileToServer", "loadOnboardingDocuments", "loadEmployeeContracts",
    ):
        assert re.search(rf"function\s+{function_name}\s*\(", html)
