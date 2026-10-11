"""오비서 UI/UX 개편 L2: 디자인 토큰 확장 + 공통 상태 컴포넌트 renderState() 정적 검사."""
import re
from pathlib import Path

ROOT = Path(__file__).parents[2] / "app/static/apps/obys"
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
CLOBE_JS = (ROOT / "modules/clobe-collection.js").read_text(encoding="utf-8")

LEGACY_TOKENS = (
    "--bg", "--panel", "--panel-strong", "--ink", "--muted", "--line", "--accent",
    "--accent-strong", "--nav", "--nav-soft", "--blue", "--red", "--amber", "--green",
)
NEW_TOKENS = {
    "--pos": "#15936b",
    "--neg": "#dc4c4c",
    "--warn": "#b7791f",
    "--radius": "14px",
    "--radius-btn": "10px",
}
DATA_VIEWS = {
    "dashboard", "tasks", "reports", "documents", "sales", "accounts", "salesLedger",
    "purchaseLedger", "bankLedger", "cardLedger", "clobeOverview", "clobeReview",
    "clobeSummary", "employees", "payroll", "inventory", "tax", "approvals", "alerts",
    "onboarding", "contracts", "attendance", "business", "integrations", "clobeSettings",
    "margins", "audit",
}


def _root_block() -> str:
    match = re.search(r":root\s*\{(.*?)\n    \}", HTML, re.S)
    assert match, ":root 블록을 찾지 못함"
    return match.group(1)


def test_legacy_root_tokens_remain():
    block = _root_block()
    for name in LEGACY_TOKENS:
        assert re.search(rf"^\s*{re.escape(name)}\s*:", block, re.M), f"{name} 삭제됨"


def test_new_root_tokens_present():
    block = _root_block()
    for name, value in NEW_TOKENS.items():
        assert re.search(rf"^\s*{re.escape(name)}\s*:\s*{re.escape(value)}\s*;", block, re.M), name
    assert "--shadow: 0 1px 2px rgba(17,24,39,.06), 0 4px 16px rgba(17,24,39,.04);" in block
    assert re.search(r"--bg:\s*#f6f7f9\s*;", block)


def test_common_component_classes_defined():
    for selector in (".kpi-card", ".todo-row", ".tag.p0", ".tag.p1", ".tag.p2",
                     ".dot.ok", ".dot.warn", ".dot.err", ".filter-bar", ".state", ".skeleton"):
        assert re.search(rf"^\s*{re.escape(selector)}\b[^{{]*\{{", HTML, re.M), selector
    kpi = re.search(r"\.kpi-card\s*\{(.*?)\}", HTML, re.S).group(1)
    assert "min-height: 118px" in kpi
    assert re.search(r"\.kpi-card b\s*\{[^}]*font-size: 28px;[^}]*font-weight: 700;[^}]*tabular-nums", HTML, re.S)
    assert "min-height: 52px" in re.search(r"\.todo-row\s*\{(.*?)\}", HTML, re.S).group(1)
    assert "dashed" in re.search(r"\.state\s*\{(.*?)\}", HTML, re.S).group(1)


def test_render_state_defined_with_five_kinds():
    assert re.search(r"function renderState\(el,", HTML)
    body = HTML[HTML.index("const STATE_DEFAULTS"):HTML.index("window.renderState = renderState")]
    for kind in ("loading", "empty", "error", "forbidden", "expired"):
        assert f'"{kind}"' in body or re.search(rf"\b{kind}:", body), kind
    assert "STATE_LOADING_DELAY_MS = 300" in body
    assert "escapeHtml(" in body
    assert "setView(action.view)" in body


def test_render_state_exposed_and_used_for_empty_review_list():
    assert "window.renderState = renderState" in HTML
    assert "window.renderState(slot" in CLOBE_JS
    assert 'data-clobe-failure="nodata"' in CLOBE_JS


def test_all_data_view_keys_remain():
    found = set(re.findall(r'data-view="([A-Za-z]+)"', HTML))
    assert DATA_VIEWS <= found, DATA_VIEWS - found


def test_pwa_block_untouched():
    for marker in ("beforeinstallprompt", "serviceWorker.register", "pwaInstallBar"):
        assert marker in HTML, marker
