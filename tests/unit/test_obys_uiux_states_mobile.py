"""오비서 UI/UX 개편 L4: 빈 상태·오류·권한 전면 적용 + 모바일 하단 탭·한 손 조작 정적 검사."""
import re
from pathlib import Path

ROOT = Path(__file__).parents[2] / "app/static/apps/obys"
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
LEDGER_JS = (ROOT / "modules/ledger-details.js").read_text(encoding="utf-8")
CLOBE_JS = (ROOT / "modules/clobe-collection.js").read_text(encoding="utf-8")

TARGET_VIEWS = {
    "sales", "salesLedger", "purchaseLedger", "bankLedger", "cardLedger", "accounts", "clobeReview",
    "clobeOverview", "employees", "payroll", "onboarding", "contracts", "attendance", "approvals", "documents",
}
# 본 화면(index.html)이 직접 그리는 목록: 화면 → 렌더 함수
INDEX_RENDERERS = {
    "sales": "function renderOperationalPages()",
    "accounts": "function renderOperationalPages()",
    "approvals": "function renderOperationalPages()",
    "documents": "function renderOperationalPages()",
    "employees": "function renderEmployees()",
    "payroll": "function renderPayroll()",
    "onboarding": "function renderOnboarding()",
    "contracts": "function renderContracts()",
    "attendance": "function renderShiftRows()",
}
BOTTOM_TABS = [("today", "오늘"), ("money", "돈 흐름"), ("review", "검토"), ("people", "사람"), ("more", "더보기")]


def _block(text: str, start: str, end: str) -> str:
    head = text.index(start)
    return text[head:text.index(end, head + len(start))]


def _function(text: str, signature: str) -> str:
    head = text.index(signature)
    nxt = re.compile(r"\n    (?:async )?function \w+\(").search(text, head + len(signature))
    return text[head:nxt.start() if nxt else len(text)]


def _media_block(width: int) -> str:
    head = HTML.index(f"@media (max-width: {width}px) {{")
    depth, i = 0, HTML.index("{", head)
    while True:
        if HTML[i] == "{":
            depth += 1
        elif HTML[i] == "}":
            depth -= 1
            if depth == 0:
                return HTML[head:i]
        i += 1


def _styles() -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", HTML, re.S))


def test_view_state_targets_cover_fifteen_views():
    body = _block(HTML, "const VIEW_STATE_TARGETS = [", "];")
    assert set(re.findall(r'"(\w+)"', body)) == TARGET_VIEWS


def test_shared_resolver_calls_render_state_with_all_causes():
    config = _function(HTML, "function viewStateConfig(view, ctx = {})")
    for needle in (
        "status === 401", 'kind: "expired"', "다시 로그인", "reloginAndRestore(view)",
        "status === 403", 'kind: "forbidden"', "이 사업자 자료를 볼 권한이 없습니다.", "관리자에게 요청",
        'kind: "error"', "viewErrorReason(error)", "code: status ? String(status)", "다시 시도",
        "확정된 거래가 아직 없습니다.", "건이 검토를 기다리고 있습니다.", "검토할 거래 열기",
        "연결되지 않았습니다.", "연결하기", "파일 올리기",
        "최근 30일 보기",
        '"자료가 없습니다"', '"새로고침"',
    ):
        assert needle in config, needle
    # 판정 순서: 401 → 403 → 그 외 오류 → (a) 검토 대기 → (b) 미연결 → (c) 기간 → 판정 불가
    order = [config.index(x) for x in ("status === 401", "status === 403", 'kind: "error"', "pending > 0", "ctx.connected === false", "ctx.periodFiltered", '"자료가 없습니다"')]
    assert order == sorted(order)
    assert "renderState(el, viewStateConfig(view, resolved))" in _function(HTML, "function renderViewState(el, view, ctx = {})")
    assert "renderViewState(tbody.querySelector" in _function(HTML, "function fillViewState(")
    assert "window.obysViewState = renderViewState" in HTML


def test_index_list_renderers_route_empty_and_errors_through_render_state():
    for view, signature in INDEX_RENDERERS.items():
        body = _function(HTML, signature)
        assert re.search(rf'fillViewState\(els\.\w+, \d, "{view}"', body), view
        assert '<div class="empty">' not in body, view
        assert "tableEmpty(" not in body or view in {"sales", "accounts", "approvals", "documents"}, view
    ops = _function(HTML, "function renderOperationalPages()")
    for gone in ("보관 문서가 없습니다.", "서버 매출·정산 데이터가 없습니다.", "등록된 계좌 또는 은행 연동이 없습니다.", "승인 대기 항목이 없습니다."):
        assert gone not in ops, gone
    assert 'fillViewState(els.shiftRows, 7, "attendance", { error: attendanceError || new Error(serverShiftsError), onRetry: attendanceRetry }, "prepend")' in HTML


def test_load_errors_are_recorded_per_view():
    set_view = _function(HTML, "function setView(view)")
    for view in ("employees", "onboarding", "contracts", "payroll", "attendance"):
        assert f'trackViewLoad("{view}"' in set_view, view
    assert 'setViewError("sales", "ledger", error)' in HTML
    assert 'setViewError("attendance", "server", error)' in HTML
    assert 'trackViewLoad("accounts", "bank", refreshBankAccounts({ silent: true }))' in HTML


def test_module_views_use_shared_state():
    for view in ("salesLedger", "purchaseLedger", "bankLedger", "cardLedger"):
        assert re.search(rf"^\s+{view}: \{{", LEDGER_JS, re.M), view
    render = _function(LEDGER_JS.replace("\n  function ", "\n    function "), "function render()")
    assert "applyViewState(root);" in render
    apply = _function(LEDGER_JS.replace("\n  function ", "\n    function "), "function applyViewState(root)")
    assert "window.obysViewState(slot, state.view, ctx)" in apply
    for key in ("pendingReview", "periodFiltered", "onRecent", "onUpload", "error:"):
        assert key in apply, key
    assert LEDGER_JS.count("data-ledger-state-slot") >= 4

    assert "applyViewSlots(root);" in CLOBE_JS and "applyEmptyState(root);" in CLOBE_JS
    assert 'window.obysViewState(slot, "clobeReview", { pendingReview: waiting' in CLOBE_JS
    assert "window.renderState(slot" in CLOBE_JS
    assert 'data-clobe-view-slot="unlinked"' in CLOBE_JS and 'data-clobe-view-slot="never-run"' in CLOBE_JS
    assert 'failureSlotHtml(state.statusError, retryAction, "status")' in CLOBE_JS
    assert 'failureSlotHtml(state.itemsError, "reload-items", "items")' in CLOBE_JS
    # 모듈 수정분이 캐시에 묶이지 않게 버전을 올렸다
    assert 'modules/ledger-details.js?v=20261014-r1"' in HTML
    assert 'modules/clobe-collection.js?v=20261014-r1"' in HTML


def test_no_standalone_no_data_phrases():
    for name, text in (("index.html", HTML), ("ledger-details.js", LEDGER_JS), ("clobe-collection.js", CLOBE_JS)):
        for phrase in ("데이터 없음", "내역이 없습니다"):
            # renderState reason 안의 설명 문장은 허용 — 단독 문구만 금지
            hits = [m.start() for m in re.finditer(phrase, text)]
            bare = [i for i in hits if "reason" not in text[max(0, i - 160):i]]
            assert not bare, (name, phrase, [text[i - 60:i + 20] for i in bare])


def test_bottom_tabs_markup_five_buttons_and_more_sheet():
    nav = _block(HTML, '<nav class="bottom-tabs" id="bottomTabs"', "</nav>")
    tabs = re.findall(r'<button type="button" class="bottom-tab" data-bottom-tab="(\w+)"[^>]*>.*?<span>([^<]+)</span></button>', nav)
    assert tabs == BOTTOM_TABS
    sheet = _block(HTML, 'id="bottomMoreSheet"', "data-bottom-sheet-close")
    assert "자료함" in sheet and "설정" in sheet
    assert set(re.findall(r'data-sheet-view="(\w+)"', sheet)) == {"documents", "business", "integrations", "clobeSettings", "audit", "settings"}
    # 하단 탭은 data-view 를 달지 않는다(기존 .tab[data-view] 바인딩·키 집합 불변)
    assert "data-view=" not in nav and "data-view=" not in sheet
    assert "syncBottomTabs();" in _function(HTML, "function setView(view)")
    assert "bindBottomTabs();" in HTML


def test_mobile_media_rules():
    mobile = _media_block(760)
    assert re.search(r"\.app-sidebar\s*\{\s*display:\s*none;", mobile)
    tabs = re.search(r"\n      \.bottom-tabs\s*\{(.*?)\}", mobile, re.S).group(1)
    assert "position: fixed" in tabs and "bottom: 0" in tabs and "display: grid" in tabs
    assert "height: calc(56px + env(safe-area-inset-bottom))" in tabs
    assert "min-height: 44px" in mobile
    assert re.search(r"button,\s*\.tab,[^{]*\{\s*min-height: 44px;", mobile)
    assert "main { padding: 12px 12px calc(56px + env(safe-area-inset-bottom) + 24px); }" in mobile
    assert ".toast { bottom: calc(56px + env(safe-area-inset-bottom) + 12px); }" in mobile
    assert ".home-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in mobile
    assert ".home-kpis > .kpi-card:first-child" in mobile and "grid-column: 1 / -1" in mobile
    # 머리글은 화면에서만 감추고(보조기기용으로 남김) 칸마다 data-label 로 붙인다
    assert ".table-wrap thead { display: none; }" not in mobile
    assert "clip: rect(0 0 0 0);" in mobile
    assert "content: attr(data-label);" in mobile
    # 위치 기반 강조(일자·거래처·금액)는 거래 표에만 — 모든 표에 일괄 적용하지 않는다
    assert not re.search(r"\.table-wrap tbody td:(?:first-child|nth-child)", mobile)
    assert '.table-wrap table[data-card="ledger"] tbody td.num { grid-column: 2;' in mobile
    # 데스크톱에서는 하단 탭을 숨긴다
    desktop = _styles().split("@media (max-width: 1120px)", 1)[0]
    assert re.search(r"\.bottom-tabs\s*\{\s*display:\s*none;\s*\}", desktop)


def test_layout_breakpoints_are_1120_and_760_only():
    styles = _styles()
    widths = {int(w) for w in re.findall(r"@media[^{]*?\(max-width:\s*(\d+)px\)", styles)}
    assert widths == {1120, 760}, widths
    assert not re.findall(r"@media[^{]*?\(min-width:", styles)
    assert "@media print {" in styles
    # 옛 900/860 규칙은 지우지 않고 이전했다
    assert ".contract-sign-layout { grid-template-columns: 1fr; }" in _media_block(1120)
    assert ".contract-a4-paper { margin: 0; }" in _media_block(760)


def test_pwa_install_bar_untouched_and_not_overlapped():
    for marker in ("beforeinstallprompt", "serviceWorker.register", "pwaInstallBar", "deferredPrompt.prompt()"):
        assert marker in HTML, marker
    header = _block(HTML, '<header class="topbar">', "</header>")
    assert 'id="pwaInstallBar"' in header
    assert 'id="bottomTabs"' not in header


def test_ui_state_persisted_with_obys_ui_prefix_and_restored():
    keys = _block(HTML, "const UI_STATE_KEYS = {", "};")
    assert set(re.findall(r'"(obys\.ui\.[\w.]+)"', keys)) == {"obys.ui.view", "obys.ui.group", "obys.ui.filters"}
    assert "`obys.ui.groupView.${group}`" in HTML
    assert "localStorage.setItem(UI_STATE_KEYS.view, view)" in HTML
    assert "localStorage.setItem(UI_STATE_KEYS.filters, JSON.stringify(filters))" in HTML
    assert "saveUiState();" in _function(HTML, "function setView(view)")
    init = _function(HTML, "async function init()")
    assert init.index("restoreUiFilters();") < init.index("renderAll();")
    assert "restoreUiState();" in init
    assert init.index("restoreUiState();") < init.index("applyAuthHashDeepLink();")
    # 재로그인 후 같은 화면으로
    assert "setView(savedUiView() || defaultView());" in _function(HTML, "async function loginToServer(")
    assert "UI_FILTER_FIELDS.forEach(id => els[id]?.addEventListener(\"change\", saveUiFilters));" in HTML


def test_mobile_rules_live_only_in_760_block():
    """리뷰 지적 2: 하단 탭·사이드 숨김·카드화·터치 영역은 760px 블록에만 있고 1120px 블록에는 없다."""
    tablet = _media_block(1120)
    for needle in (".bottom-tabs", ".bottom-sheet", ".app-sidebar", ".table-wrap", "min-height: 44px"):
        assert needle not in tablet, needle
    mobile = _media_block(760)
    for needle in (".bottom-tabs {", ".bottom-sheet:not(.hidden) { display: flex; }", ".app-sidebar {", ".table-wrap tbody tr {"):
        assert needle in mobile, needle


def test_table_cards_label_cells_from_headers():
    """리뷰 지적 4: 열 구성이 다른 표(승인·근태)는 위치로 추측하지 않고 머리글 라벨을 붙인다."""
    body = _function(HTML, "function labelTableCells(root = document)")
    assert "td.dataset.label = label" in body
    assert 'table.dataset.card = "ledger"' in body and "delete table.dataset.card" in body
    assert "tbody tr:not(.state-row)" in body
    date_head = re.search(r"const CARD_DATE_HEAD = /\^\((.*?)\)\$/;", HTML).group(1).split("|")
    party_head = re.search(r"const CARD_PARTY_HEAD = /\^\((.*?)\)\$/;", HTML).group(1).split("|")
    assert "일자" in date_head and "거래처" in party_head
    assert "업무" not in date_head  # 승인 표(업무·대상·상태·처리 화면)는 ledger 카드가 아니다
    observer = _function(HTML, "function bindTableCardLabels()")
    assert "labelTableCells();" in observer and "MutationObserver" in observer
    assert "bindBottomTabs();\n      bindTableCardLabels();" in HTML


def test_bottom_sheet_is_modal_above_tabs_and_desktop_hidden():
    """리뷰 지적 5·6: 시트는 탭 위(z-index), 데스크톱에서는 숨김, 포커스 트랩과 포커스 복귀가 있다."""
    styles = _styles()
    base = re.search(r"\n    \.bottom-sheet \{(.*?)\}", styles, re.S).group(1)
    tabs = re.search(r"\n      \.bottom-tabs\s*\{(.*?)\}", _media_block(760), re.S).group(1)
    sheet_z = int(re.search(r"z-index: (\d+);", base).group(1))
    tabs_z = int(re.search(r"z-index: (\d+);", tabs).group(1))
    assert sheet_z > tabs_z
    assert "display: none;" in base  # 760px 초과에서는 hidden 이 풀려도 보이지 않는다
    assert ".bottom-sheet.hidden { display: none; }" not in styles
    trap = _function(HTML, "function trapBottomSheetFocus(event)")
    assert 'event.key !== "Tab"' in trap and "first.focus();" in trap and "last.focus();" in trap
    bind = _function(HTML, "function bindBottomTabs()")
    assert "trapBottomSheetFocus(event);" in bind
    assert 'matchMedia?.("(max-width: 760px)")' in bind and "toggleBottomSheet(false)" in bind
    assert "trigger?.focus();" in _function(HTML, "function toggleBottomSheet(open)")


def test_track_view_load_callers_do_not_chain():
    """리뷰 지적 7: trackViewLoad 는 실패를 처리하고 resolve 한다 — 호출부가 .catch/.then/await 를 잇지 않는다."""
    assert not re.search(r"trackViewLoad\([^;\n]*\)\s*\.(?:catch|then)\(", HTML)
    assert "await trackViewLoad(" not in HTML
    body = _function(HTML, "function trackViewLoad(view, key, promise, toastLabel = \"\")")
    assert "setViewError(view, key, error);" in body


def test_with_josa_guards_empty_input():
    """리뷰 지적 8: 빈 입력은 NaN 판정으로 '가'가 붙지 않도록 일반 명사로 바꾼다."""
    body = _function(HTML, "function withJosa(word, consonant, vowel)")
    assert '|| "자료"' in body
    assert "!Number.isFinite(code)" in body
