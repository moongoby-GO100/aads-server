"""오비서 UI/UX 개편 L3: 오늘 홈(5지표+처리할 일) + 메뉴 27→5묶음 재편 정적 검사."""
import re
from pathlib import Path

ROOT = Path(__file__).parents[2] / "app/static/apps/obys"
HTML = (ROOT / "index.html").read_text(encoding="utf-8")

DATA_VIEWS = {
    "dashboard", "tasks", "reports", "documents", "sales", "accounts", "salesLedger",
    "purchaseLedger", "bankLedger", "cardLedger", "clobeOverview", "clobeReview",
    "clobeSummary", "employees", "payroll", "inventory", "tax", "approvals", "alerts",
    "onboarding", "contracts", "attendance", "business", "integrations", "clobeSettings",
    "margins", "audit",
}
GROUPS = {
    "today": ("오늘", {"dashboard", "tasks", "alerts"}),
    "money": ("돈 흐름", {"sales", "salesLedger", "accounts", "bankLedger", "cardLedger", "purchaseLedger", "inventory"}),
    "review": ("검토·마감", {"clobeReview", "clobeOverview", "clobeSummary", "reports", "closing", "tax", "margins"}),
    "people": ("사람", {"employees", "attendance", "payroll", "onboarding", "contracts", "approvals"}),
    "vault": ("자료함", {"documents"}),
    "settings": ("설정", {"business", "integrations", "clobeSettings", "audit", "settings"}),
}
HOME_API_PATHS = (
    "/sales", "/settlements", "/bank-summary", "/accounts", "/reviews", "/payroll",
    "/employees/join-requests", "/onboarding/documents", "/contracts/pending-signature",
    "/collection-status",
)


def _block(start: str, end: str) -> str:
    head = HTML.index(start)
    return HTML[head:HTML.index(end, head)]


def _side_nav() -> str:
    return _block('<nav class="side-nav"', "</nav>")


def _dashboard() -> str:
    return _block('<section id="dashboardView">', '<section id="tasksView"')


def _home_js() -> str:
    return _block("async function loadTodayHome(", "function bindTodayHome(")


def test_all_data_view_keys_still_reachable():
    found = set(re.findall(r'data-view="([A-Za-z]+)"', HTML))
    assert DATA_VIEWS <= found, DATA_VIEWS - found
    side = set(re.findall(r'data-view="([A-Za-z]+)"', _side_nav()))
    assert DATA_VIEWS <= side, DATA_VIEWS - side


def test_side_nav_has_five_groups_plus_settings_in_prd_order():
    side = _side_nav()
    labels = re.findall(r'<div class="side-label[^"]*" data-nav-group="(\w+)">([^<]+)</div>', side)
    assert labels == [(key, label) for key, (label, _) in GROUPS.items()]
    for key, (_, views) in GROUPS.items():
        tagged = set(re.findall(rf'data-view="([A-Za-z]+)" data-group="{key}"', side))
        assert tagged == views, (key, tagged ^ views)


def test_side_nav_payroll_once_and_relabeled():
    side = _side_nav()
    assert side.count('data-view="payroll"') == 1
    assert "급여내역서" not in side
    for view, label in {
        "dashboard": "오늘 홈",
        "clobeReview": "검토할 거래",
        "clobeOverview": "자료 현황",
        "clobeSummary": "경영 요약",
        "clobeSettings": "자료 연결",
        "audit": "회원·권한",
    }.items():
        assert re.search(rf'data-view="{view}"[^>]*>{re.escape(label)}</button>', side), view
    # clobeReview 가 검토·마감 묶음 맨 앞
    review = side.split('data-nav-group="review"', 1)[1]
    assert re.search(r'data-view="(\w+)"', review).group(1) == "clobeReview"


def test_top_quick_tabs_replaced_by_group_sub_tabs():
    assert '<nav class="tabs"' not in HTML
    assert "상단 빠른 화면" not in HTML
    assert not re.search(r"^\s*\.tabs\s*\{", HTML, re.M)
    sub = _block('<nav class="sub-tabs" id="subTabs"', "</nav>")
    assert set(re.findall(r'data-view="([A-Za-z]+)"', sub)) >= DATA_VIEWS
    for key in GROUPS:
        assert f'.sub-tabs[data-active-group="{key}"] > .tab:not([data-group="{key}"])' in HTML, key
    # 기존 tab 전환 로직 재사용: setView 가 묶음만 동기화한다
    set_view = _block("function setView(view) {", "applyAccessPolicy();\n    }")
    assert "syncSubTabs();" in set_view
    assert 'document.querySelector(`.side-nav [data-view="${currentView}"]`)?.dataset.group' in HTML


def test_home_fixed_cards_removed_and_five_kpi_cards_present():
    dash = _dashboard()
    for gone in ("ops-status-grid", "approval-grid", "dashboard-hero", "unni-shortcuts"):
        assert gone not in dash, gone
    labels = re.findall(r'class="kpi-card"[^>]*data-home-kpi="\w+"[^>]*>\s*<small>([^<]+)</small>', dash)
    assert labels == ["오늘 매출", "통장 입출금", "검토 대기", "급여일", "승인 대기"]
    assert 'id="homeBusinessSelect"' in dash and 'id="homeToday"' in dash and 'id="homeCollectStatus"' in dash
    assert 'id="homeTodoRows"' in dash
    # 월 KPI 띠·월마감·최근 입력 내역 유지, 처리할 일 → 매장 지표 순서
    order = [dash.index(x) for x in ('id="homeKpiGrid"', 'id="homeTodoRows"', "이번 달 매장 지표", 'id="kpiGrid"', 'id="progressList"', 'id="recentRows"')]
    assert order == sorted(order)


def test_unni_shortcuts_moved_to_documents_view():
    docs = _block('<section id="documentsView"', '<section id="salesView"')
    assert 'class="unni-shortcuts"' in docs
    assert HTML.count('class="unni-shortcuts"') == 1


def test_home_aggregation_uses_allsettled_and_existing_api_only():
    js = _home_js()
    assert "Promise.allSettled([" in js
    assert "Promise.all(" not in js
    for path in HOME_API_PATHS:
        assert f'financeApi(`{path}' in js or f'financeApi("{path}' in js, path
    called = re.findall(r'financeApi\([`"](/[a-z\-/]+)', js)
    assert set(called) <= set(HOME_API_PATHS), set(called) - set(HOME_API_PATHS)
    assert "failHomeCard(" in js
    assert 'renderState(card.querySelector("[data-kpi-state]"), { kind: "error"' in HTML
    assert "최근 7일" in js and "전체 ${pending.length" in js


def test_todo_list_limit_priorities_and_empty_state():
    assert "HOME_TODO_LIMIT = 7" in HTML
    todos = _block("function renderHomeTodos(", "function openHomeTarget(")
    assert 'class="todo-row"' in todos
    assert 'renderState(el, { kind: "empty", title: "오늘 처리할 일이 없습니다" })' in todos
    for tag in ('"긴급"', '"오늘"', '"이번 주"'):
        assert tag in HTML, tag
    js = _home_js()
    for title in ("가입 요청", "입사서류", "서명 대기", "검토 대기"):
        assert title in js, title
    assert "수집 멈춤" in HTML


def test_kpi_cards_navigate_with_filters():
    bind = _block("function bindTodayHome(", "function syncSubTabs(")
    assert '{ sales: "sales", bank: "bankLedger", review: "clobeReview", payroll: "payroll", approval: "approvals" }' in bind
    opener = _block("function openHomeTarget(", "async function loadTodayHome(")
    assert "els.salesDateFrom.value = today" in opener and "setView(view)" in opener


def test_month_store_kpi_band_items():
    kpis = _block("function renderKpis()", "function renderProgress()")
    for label in ("배달 정산 차이", "식재료율", "인건비율", "추정 손익"):
        assert f'["{label}"' in kpis, label


def test_pwa_block_untouched():
    for marker in ("beforeinstallprompt", "serviceWorker.register", "pwaInstallBar"):
        assert marker in HTML, marker
