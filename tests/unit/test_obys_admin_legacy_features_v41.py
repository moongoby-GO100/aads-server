"""오비서 V4.1 새 관리화면 — 기존 관리화면에만 있던 업무 화면 이전 계약.

대상: 자료 수집 현황(clobe-collection) · 원장 상세(ledger-details) · 매장비서(store-assistant-v2)
      · 월마감 · 기초 설정·계좌 등록.

정적 검사(메뉴 등록·모듈 로드·기존 관리화면 전역 의존 0건)와, node 로 실제 모듈을 실행해
host 모드에서 기존 관리화면 전역·요소를 한 번도 읽지 않는지 확인한다(네트워크·DB 없음).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OBYS = ROOT / "app" / "static" / "apps" / "obys"
ADMIN = (OBYS / "admin.html").read_text(encoding="utf-8")
INDEX = (OBYS / "index.html").read_text(encoding="utf-8")
MOD = OBYS / "modules"
EXTRAS = (MOD / "admin-extras-v41.js").read_text(encoding="utf-8")
EXTRAS_CSS = (MOD / "admin-extras-v41.css").read_text(encoding="utf-8")
NODE = shutil.which("node")

MOVED = {
    "store-assistant": ("경영", "매장비서"),
    "ledger-details": ("세무·회계", "원장 상세"),
    "month-close": ("세무·회계", "월마감"),
    "data-collection": ("설정", "자료 수집 현황"),
    "basic-settings": ("설정", "기초 설정·계좌 등록"),
}
# 기존 관리화면(index.html)이 제공하던 전역·요소. 새 화면 경로에서는 읽으면 안 된다.
LEGACY_GLOBALS = ["obysViewState", "renderState", "branchByName", "selectedSettingsBusinessId", "YEOLJEONG_STORE_ASSISTANT_CONFIG"]
LEGACY_IDS = ["loginBtn", "openLoginFromGateBtn", "branchFilter", "monthFilter", "appMain", "authGate", "settingsBusinessSelect"]


def _inline_script() -> str:
    blocks = re.findall(r"<script>(.*?)</script>", ADMIN, re.S)
    assert len(blocks) == 1
    return blocks[0]


def _menu():
    return re.search(r"const menu=(\[.*?\n    \]);", ADMIN, re.S).group(1)


def _node(code: str) -> str:
    if not NODE:
        pytest.skip("node 없음: 실행 검증 생략")
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    return r.stdout


# ── 메뉴 등록 ────────────────────────────────────────────────

def test_menu_registers_every_moved_feature_in_the_requested_group():
    out = _node(f"const menu={_menu()};console.log(JSON.stringify(menu.map(g=>[g.label,g.items])))")
    groups = {label: dict((route, name) for name, route in items) for label, items in json.loads(out)}
    for route, (group, name) in MOVED.items():
        assert groups[group].get(route) == name, (route, group)
    # 기존 메뉴는 그대로 남는다.
    assert groups["설정"]["integrations"] == "자동연동 관리"
    assert groups["세무·회계"]["journals"] == "전표관리"
    assert len(groups) == 8


def test_moved_screen_names_are_business_names_not_internal_names():
    out = _node(f"const menu={_menu()};console.log(JSON.stringify(menu.flatMap(g=>g.items.map(x=>x[0]))))")
    routes_block = EXTRAS.split("const ROUTES = {", 1)[1].split("\n  };", 1)[0]
    titles = re.findall(r'title: "([^"]+)"', routes_block)
    assert len(titles) == len(MOVED)
    tab_labels = re.findall(r'\["[A-Za-z]+", "([^"]+)"\]', routes_block)
    assert len(tab_labels) == 8
    for text in json.loads(out) + titles + tab_labels:
        assert re.search(r"[가-힣]", text), text
        for internal in ("clobe", "클로브", "ledger", "store-assistant", "v2", "settings", "closing", "Ledger"):
            assert internal not in text, (internal, text)


def test_basic_settings_menu_is_owner_admin_only_and_server_route_set_untouched():
    fn = next(s.strip() for s in ADMIN.splitlines() if s.strip().startswith("function canReadRouteV41("))
    mgr = next(s.strip() for s in ADMIN.splitlines() if s.strip().startswith("const MANAGER_READ_ROUTES="))
    assert "basic-settings" not in mgr  # 서버 ROUTE_READ_ROLES 와 같은 집합은 바꾸지 않는다.
    out = _node(f"const state={{}};{mgr}{fn}const r={{}};for(const role of ['owner','admin','member','viewer',null]){{state.readRole=role;r[role]=[canReadRouteV41('basic-settings'),canReadRouteV41('month-close'),canReadRouteV41('data-collection')]}}console.log(JSON.stringify(r))")
    got = json.loads(out)
    assert got["owner"] == [True, True, True] and got["admin"] == [True, True, True]
    assert got["member"][0] is False and got["viewer"][0] is False and got["null"][0] is False
    assert got["member"][1:] == [True, True]


# ── 모듈 로드 ────────────────────────────────────────────────

def test_admin_loads_moved_modules_in_host_mode_before_inline_script():
    inline_at = ADMIN.index("<script>\n")
    for name in ("clobe-collection", "ledger-details", "store-assistant-v2"):
        m = re.search(rf'<script src="/static/apps/obys/modules/{name}\.js\?v=\d{{8}}-r\d+" data-obys-host="admin-v41"></script>', ADMIN)
        assert m, name
        assert m.start() < inline_at, name
    m = re.search(r'<script src="/static/apps/obys/modules/admin-extras-v41\.js\?v=\d{8}-r\d+"></script>', ADMIN)
    assert m and m.start() < inline_at
    for css in ("clobe-collection", "ledger-details", "admin-extras-v41"):
        assert re.search(rf'<link rel="stylesheet" href="modules/{css}\.css\?v=\d{{8}}-r\d+">', ADMIN), css
    # 기존 매장비서 CSS 는 전역 규칙(.panel·.badge·button 높이)을 바꾸므로 새 화면에 싣지 않는다.
    assert "store-assistant-v2.css" not in ADMIN
    # 개발 문서 링크 설정·직원 화면 설치 집계는 화면 기능이 아니므로 옮기지 않는다.
    assert "app-config.js" not in ADMIN and "app-install-ping.js" not in ADMIN


def test_extras_css_is_scoped_to_moved_screens():
    for rule in re.findall(r"^([^@{}/\s][^{]*)\{", EXTRAS_CSS, re.M):
        for selector in rule.split(","):
            assert selector.strip().startswith("[data-admin-extra]"), selector


def test_legacy_index_still_loads_modules_without_host_mode():
    # index.html 삭제는 별도 작업이다. 기존 관리화면은 그대로 돈다.
    assert "modules/ledger-details.js" in INDEX and "modules/store-assistant-v2.js" in INDEX
    assert "modules/clobe-collection.js" in INDEX
    assert "data-obys-host" not in INDEX


# ── 기존 관리화면 전역 의존 0건 ──────────────────────────────

def test_admin_path_has_zero_legacy_global_references():
    inline = _inline_script()
    for token in LEGACY_GLOBALS + LEGACY_IDS + ["yeoljeong-finance-auth-user"]:
        # 새 연결 모듈은 기존 전역을 window·globalThis 로 읽지 않는다. (renderState 는 여기서 지역 함수 이름으로도 쓴다.)
        assert not re.search(rf"(window|globalThis)\.{re.escape(token)}\b", EXTRAS), token
        if token != "renderState":
            assert not re.search(rf"\b{re.escape(token)}\b", EXTRAS), token
        assert f"window.{token}" not in inline and f'"{token}"' not in inline, token
    # 모듈 안의 기존 전역 읽기는 host 모드 판정 뒤에만 있다.
    for name, tokens in {
        "clobe-collection.js": ["window.obysViewState", "window.renderState"],
        "ledger-details.js": ["window.obysViewState"],
        "store-assistant-v2.js": ["window.branchByName", "window.selectedSettingsBusinessId"],
    }.items():
        js = (MOD / name).read_text(encoding="utf-8")
        assert 'const HOST_MODE = String(document.currentScript?.dataset?.obysHost || "");' in js, name
        for token in tokens:
            for m in re.finditer(re.escape(token), js):
                guard = js[max(0, m.start() - 400):m.start()]
                assert "HOST_MODE" in guard, (name, token)


HARNESS = r"""
const touched = [], domIds = [], calls = [], errors = [];
process.on("unhandledRejection", e => errors.push(String(e && e.stack || e)));
for (const k of %(globals)s) Object.defineProperty(globalThis, k, { get() { touched.push(k); return () => {}; }, configurable: true });
globalThis.window = globalThis;
const roots = [];
function El(id = "") {
  const el = { id, dataset: {}, style: { setProperty() {}, removeProperty() {} }, _html: "", isConnected: true, value: "", hidden: false, disabled: false, elements: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    querySelector() { return El(); }, querySelectorAll() { return []; }, closest() { return null; }, matches() { return false; },
    append() {}, before() {}, remove() {}, focus() {}, scrollIntoView() {}, reset() {}, insertAdjacentHTML() {} };
  Object.defineProperty(el, "innerHTML", { get() { return el._html; }, set(v) { el._html = String(v); }, configurable: true });
  return el;
}
const store = { getItem: k => (k === "aads_token" ? "tok" : null), setItem() {}, removeItem() {} };
globalThis.localStorage = store; globalThis.sessionStorage = store;
globalThis.document = {
  currentScript: { dataset: { obysHost: "admin-v41" } }, readyState: "complete", cookie: "", visibilityState: "visible", activeElement: null,
  body: { classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } }, dataset: {}, append() {} },
  getElementById(id) { domIds.push(id); return roots.find(r => r.id === id) || null; },
  querySelector(sel) { domIds.push(sel); return null; }, querySelectorAll() { return []; },
  addEventListener() {}, createElement() { return El(); }
};
globalThis.addEventListener = () => {};
globalThis.fetch = async (url, opts = {}) => { calls.push(String(url)); return { ok: true, status: 200, headers: { get: () => "" }, json: async () => (%(payload)s) }; };
"""


def _run_module(name: str, body: str, payload: str = "{}") -> dict:
    src = (MOD / name).read_text(encoding="utf-8")
    code = HARNESS % {"globals": json.dumps(LEGACY_GLOBALS), "payload": payload} + f"\n{src}\n" + body + """
setTimeout(() => console.log(JSON.stringify({ touched, domIds, calls, errors, out: globalThis.__out || null })), 50);
"""
    return json.loads(_node(code))


def _assert_no_legacy(result):
    assert result["touched"] == [], result["touched"]
    assert not [x for x in result["domIds"] if x in LEGACY_IDS or "side-nav" in str(x) or ".tab" in str(x)], result["domIds"]
    assert result["errors"] == [], result["errors"]


def test_clobe_mount_runs_without_legacy_globals_and_routes_navigation_to_host():
    result = _run_module("clobe-collection.js", """
const el = El(); roots.push(el); const gone = [];
const ok = window.obysClobe.mount(el, "clobeOverview", { role: "owner", viewState() {}, renderState() {}, goView: v => { gone.push(v); return true; }, login() {} });
globalThis.__out = { ok, id: el.id, html: el.innerHTML.length > 0, gone, views: window.obysClobe.views };
""", payload='{"companies": []}')
    _assert_no_legacy(result)
    out = result["out"]
    assert out["ok"] is True and out["id"] == "clobeOverviewView" and out["html"] is True
    assert out["views"] == ["clobeOverview", "clobeReview", "clobeSummary", "clobeSettings"]
    assert any(c.startswith("/api/v1/obys-collections/status") for c in result["calls"])


def test_ledger_mount_uses_host_business_and_existing_api_only():
    result = _run_module("ledger-details.js", """
const el = El(); roots.push(el);
const ok = window.obysLedgerDetails.mount(el, "salesLedger", { businessId: "b2", viewState() {} });
globalThis.__out = { ok, id: el.id };
""", payload='{"businesses": [{"id": "b1", "name": "가"}, {"id": "b2", "name": "나"}], "rows": [], "uploads": []}')
    _assert_no_legacy(result)
    assert result["out"] == {"ok": True, "id": "salesLedgerView"}
    assert all(c.startswith("/api/v1/") for c in result["calls"]), result["calls"]
    assert any("tenant-registry/businesses" in c for c in result["calls"])
    assert any("b2" in c for c in result["calls"] if "tenant-registry" not in c), result["calls"]


def test_store_assistant_mount_scopes_to_host_business_and_skips_legacy_wiring():
    result = _run_module("store-assistant-v2.js", """
const el = El(); let expired = 0;
const ok = window.ObysStoreAssistant.mount(el, { businessId: "b9", onExpired() { expired += 1; } });
globalThis.__out = { ok, html: el.innerHTML.includes('id="inventoryOrderForm"') && el.innerHTML.includes('id="kpiGrid"'), expired };
""", payload='{"kpis": {}, "tasks": [], "items": [], "orders": [], "stock_balances": [], "tax_reports": []}')
    _assert_no_legacy(result)
    assert result["out"]["ok"] is True and result["out"]["html"] is True
    reads = [c for c in result["calls"] if "/api/v1/yeoljeong-" in c]
    assert {c.split("?")[0] for c in reads} >= {
        "/api/v1/yeoljeong-dashboard/kpis", "/api/v1/yeoljeong-dashboard/tasks",
        "/api/v1/yeoljeong-inventory/items", "/api/v1/yeoljeong-inventory/orders",
        "/api/v1/yeoljeong-accounting/summary", "/api/v1/yeoljeong-accounting/tax-reports",
    }
    assert all("business_id=b9" in c for c in reads), reads


# ── 새 화면 연결(admin-extras-v41.js) ───────────────────────

def _extras(body: str, payload: str = "{}") -> dict:
    return _run_module("admin-extras-v41.js", body, payload)


def test_extras_navigation_maps_module_views_to_new_menu_and_tabs():
    result = _extras("""
const X = window.ObysAdminExtras; const page = El(); const routes = []; const mounted = [];
window.obysClobe = { mount(el, view, o) { mounted.push(view); globalThis.__go = o.goView; return true; } };
const ctx = { businessId: "b1", businessName: "가", role: "owner", authHeaders: () => ({}), financeApi: async () => ({}), goRoute: r => routes.push(r), toast() {}, onExpired() {} };
X.render("data-collection", page, ctx);
const g = globalThis.__go;
const a = g("documents"), b = g("integrations"), c = g("business"), d = g("clobeReview"), e = g("salesLedger"), f = g("unknownView");
globalThis.__out = { routes, mounted, results: [a, b, c, d, e, f], has: X.routes.map(r => X.has(r)), title: X.title("month-close") };
""")
    _assert_no_legacy(result)
    out = result["out"]
    assert out["routes"] == ["documents", "integrations", "businesses", "ledger-details"]
    assert out["mounted"] == ["clobeOverview", "clobeReview"]
    assert out["results"] == [True, True, True, True, True, False]
    assert all(out["has"]) and out["title"] == "월마감"


def test_extras_render_is_idempotent_for_same_route_business_and_tab():
    result = _extras("""
const X = window.ObysAdminExtras; let n = 0; const extra = { dataset: { key: "" } };
const page = El(); page.querySelector = sel => (sel.includes("data-admin-extra") && page._html ? extra : El());
Object.defineProperty(page, "innerHTML", { get() { return page._html; }, set(v) { n += 1; page._html = v; extra.dataset.key = (v.match(/data-key="([^"]+)"/) || [])[1] || ""; } });
window.ObysStoreAssistant = { mount() { return true; } };
const ctx = { businessId: "b1", role: "owner", goRoute() {}, onExpired() {} };
X.render("store-assistant", page, ctx); X.render("store-assistant", page, ctx); const same = n;
X.render("store-assistant", page, { ...ctx, businessId: "b2" });
globalThis.__out = { same, after: n };
""")
    _assert_no_legacy(result)
    assert result["out"] == {"same": 1, "after": 2}


def test_month_close_never_turns_failed_lookups_into_zero_and_judges_conservatively():
    result = _extras("""
const X = window.ObysAdminExtras;
const ok = data => ({ ok: true, data }), bad = { ok: false, error: new Error("x") };
const all = X.closingChecks("b1", {
  summary: ok({ total_sales: 1200000, total_purchases: 400000, total_expenses: 100000, operating_profit: 700000 }),
  settlement: ok({ summary: [{ pending_count: 0, pending_amount: 0 }] }),
  collections: ok({ companies: [{ business_id: "b1", link_status: "linked", item_stage_counts: { review_box: 0, reviewed: 0 } }, { business_id: "zz", link_status: "linked", item_stage_counts: { review_box: 9 } }] }),
  approvals: ok({ pending: 0 }),
  corrections: ok({ items: [{ status: "approved", requested: { work_date: "2026-11-03" } }] })
}, "2026-11");
const fail = X.closingChecks("b1", { summary: bad, settlement: ok({ summary: [{ pending_count: 2, pending_amount: 30000 }] }), collections: ok({ companies: [] }), approvals: bad, corrections: bad }, "2026-11");
const unknown = X.closingChecks("b1", { summary: ok({ total_sales: 5 }), settlement: ok({ summary: [] }), collections: bad, approvals: ok({ pending: 0 }), corrections: ok({ items: [] }) }, "2026-11");
const noSales = X.closingChecks("b1", { summary: ok({ total_sales: 0 }), settlement: ok({ summary: [] }), collections: ok({ companies: [] }), approvals: ok({ pending: 0 }), corrections: ok({ items: [] }) }, "2026-11");
globalThis.__out = { all, fail, unknown, noSales, report: X.closingReport("2026-11", "가", fail), range: X.monthRange("2026-02"), leap: X.monthRange("2028-02"), badRange: X.monthRange("2026-13"),
  paths: X.closingPaths("b 1", "2026-11") };
""")
    _assert_no_legacy(result)
    out = result["out"]
    assert out["all"]["verdict"] == "기간 구분 없는 현재 상태는 선택 월 확인 후 판정"
    assert [c["status"] for c in out["all"]["checks"]] == ["pass", "hold", "hold", "hold", "pass"]
    # 조회 실패는 0 으로 바뀌지 않고 확인 불가로 남는다(판정 보류가 마감 가능으로 새지 않는다).
    statuses = {c["key"]: c["status"] for c in out["fail"]["checks"]}
    assert statuses == {"sales": "unknown", "settlement": "hold", "review": "skip", "approvals": "unknown", "corrections": "unknown"}
    assert out["fail"]["verdict"] == "일부 자료를 확인하지 못해 판정 보류"
    assert out["unknown"]["verdict"] == "일부 자료를 확인하지 못해 판정 보류"
    # 선택 월 자료(매출)가 실패하면 마감 불가.
    assert out["noSales"]["verdict"] == "추가 확인 필요"
    report = out["report"]
    assert "매출: 확인 불가" in report and "매출: 0원" not in report and "마감 판정: 일부 자료를 확인하지 못해 판정 보류" in report
    assert out["range"] == {"from": "2026-02-01", "to": "2026-02-28"} and out["leap"]["to"] == "2028-02-29"
    assert out["badRange"] is None
    paths = out["paths"]
    assert paths["summary"] == "/api/v1/yeoljeong-accounting/summary?business_id=b%201&date_from=2026-11-01&date_to=2026-11-30"
    assert paths["corrections"] == "/api/v1/workspaces/b%201/attendance/corrections"
    assert all(p.startswith("/api/v1/") for p in paths.values())


def test_month_close_correction_list_at_server_cap_never_confirms_zero():
    """리뷰 지적 1: 서버 정정 목록은 최신 500건만 준다. 500건이 모두 처리 완료여도 0건으로 확정하지 않는다."""
    service = (ROOT / "app/services/obys_attendance_corrections.py").read_text(encoding="utf-8")
    assert "ORDER BY created_at DESC LIMIT 500" in service  # 화면 상한값이 서버 상한과 같은지 고정
    result = _extras("""
const X = window.ObysAdminExtras;
const ok = data => ({ ok: true, data });
const base = { summary: ok({ total_sales: 100 }), settlement: ok({ summary: [] }), collections: ok({ companies: [] }), approvals: ok({ pending: 0 }) };
const done = n => Array.from({ length: n }, () => ({ status: "approved", requested: { work_date: "2026-11-02" } }));
const capped = X.closingChecks("b1", { ...base, corrections: ok({ items: done(500) }) }, "2026-11");
const under = X.closingChecks("b1", { ...base, corrections: ok({ items: done(499) }) }, "2026-11");
const cappedPending = X.closingChecks("b1", { ...base, corrections: ok({ items: [...done(499), { status: "pending", requested: { work_date: "2026-11-05" } }] }) }, "2026-11");
const noItems = X.closingChecks("b1", { ...base, corrections: ok({}) }, "2026-11");
globalThis.__out = { limit: X.CORRECTIONS_LIST_LIMIT, capped, under, cappedPending, noItems };
""")
    _assert_no_legacy(result)
    out = result["out"]
    assert out["limit"] == 500
    corr = lambda r: next(c for c in r["checks"] if c["key"] == "corrections")
    assert corr(out["capped"])["status"] == "unknown" and "500" in corr(out["capped"])["detail"]
    assert out["capped"]["verdict"] != "마감 가능"
    assert corr(out["under"])["status"] == "pass" and out["under"]["verdict"] != "마감 가능"
    assert corr(out["cappedPending"])["status"] == "fail" and "최소" in corr(out["cappedPending"])["detail"]
    assert corr(out["noItems"])["status"] == "unknown"


def test_month_close_judges_selected_month_and_does_not_fail_on_other_months():
    """리뷰 지적 2: 기간 없는 정산·승인·검토 수치와 다른 달 정정 대기로 선택 월 마감을 실패시키지 않는다."""
    result = _extras("""
const X = window.ObysAdminExtras;
const ok = data => ({ ok: true, data });
const linked = n => ok({ companies: [{ business_id: "b1", link_status: "linked", item_stage_counts: { review_box: n } }] });
const novPending = { status: "pending", requested: JSON.stringify({ work_date: "2026-11-04" }), original: JSON.stringify({ work_date: "2026-11-04" }) };
const octPending = { status: "pending", requested: { work_date: "2026-10-31" }, original: { work_date: "2026-10-31" } };
const movedIntoOct = { status: "pending", requested: { work_date: "2026-11-01" }, original: { work_date: "2026-10-30" } };
const base = { summary: ok({ total_sales: 100 }), settlement: ok({ summary: [{ pending_count: 0 }] }), collections: linked(0), approvals: ok({ pending: 0 }) };
const octWithNovCorrection = X.closingChecks("b1", { ...base, corrections: ok({ items: [novPending] }) }, "2026-10");
const novWithNovCorrection = X.closingChecks("b1", { ...base, corrections: ok({ items: [novPending] }) }, "2026-11");
const octWithOctCorrection = X.closingChecks("b1", { ...base, corrections: ok({ items: [octPending] }) }, "2026-10");
const octWithMoved = X.closingChecks("b1", { ...base, corrections: ok({ items: [movedIntoOct] }) }, "2026-10");
const undated = X.closingChecks("b1", { ...base, corrections: ok({ items: [{ status: "pending" }] }) }, "2026-10");
const octWithNovUnpaid = X.closingChecks("b1", { ...base, settlement: ok({ summary: [{ pending_count: 3, pending_amount: 9000 }] }), approvals: ok({ pending: 2 }), collections: linked(4), corrections: ok({ items: [] }) }, "2026-10");
globalThis.__out = { octWithNovCorrection, novWithNovCorrection, octWithOctCorrection, octWithMoved, undated, octWithNovUnpaid,
  report: X.closingReport("2026-10", "가", octWithNovUnpaid) };
""")
    _assert_no_legacy(result)
    out = result["out"]
    by = lambda r: {c["key"]: c for c in r["checks"]}
    # 11월 근무 정정 대기는 10월 마감을 막지 않는다.
    assert by(out["octWithNovCorrection"])["corrections"]["status"] == "pass"
    assert "다른 달 대기 1건" in by(out["octWithNovCorrection"])["corrections"]["detail"]
    assert out["octWithNovCorrection"]["verdict"] == "기간 구분 없는 현재 상태는 선택 월 확인 후 판정"
    for key in ("settlement", "approvals", "review"):
        assert by(out["octWithNovCorrection"])[key]["status"] == "hold"
        assert "선택 월" in by(out["octWithNovCorrection"])[key]["detail"]
    assert by(out["novWithNovCorrection"])["corrections"]["status"] == "fail"
    assert by(out["octWithOctCorrection"])["corrections"]["status"] == "fail"
    assert by(out["octWithMoved"])["corrections"]["status"] == "fail"  # 원래 근무일이 10월이면 10월 건
    assert by(out["undated"])["corrections"]["status"] == "unknown"
    # 기간 없는 현재 미처리 건은 실패가 아니라 "해당 월 확인 필요" 이고, 마감 가능으로도 새지 않는다.
    checks = by(out["octWithNovUnpaid"])
    assert {k: checks[k]["status"] for k in ("settlement", "approvals", "review")} == {"settlement": "hold", "approvals": "hold", "review": "hold"}
    assert {k: checks[k]["scope"] for k in checks} == {"sales": "month", "settlement": "all", "review": "all", "approvals": "all", "corrections": "month"}
    assert out["octWithNovUnpaid"]["verdict"] not in ("추가 확인 필요", "마감 가능")
    assert "정산 미입금 0건 [현재 상태·기간 미구분]: 해당 월 확인 필요" in out["report"]
    assert "이번 달 매출 집계 [선택 월]: 통과" in out["report"]


def test_month_close_screen_passes_selected_month_and_shows_scope():
    block = EXTRAS.split("async function renderMonthClose", 1)[1].split("// ── 기초 설정", 1)[0]
    assert "closingChecks(ctx.businessId, Object.fromEntries(entries), month)" in block
    assert "data-scope=" in block and "CLOSING_SCOPE_LABEL" in block


def test_month_close_is_read_only():
    block = EXTRAS.split("// ── 월마감", 1)[1].split("// ── 기초 설정", 1)[0]
    assert "method:" not in block and '"POST"' not in block and '"PUT"' not in block


def test_account_registration_appends_one_account_and_refuses_unsafe_saves():
    result = _extras("""
const X = window.ObysAdminExtras; const id = () => "acct-test";
const fresh = { settings: { businesses: [{ id: "b1", name: "가" }], branches: [{ id: "r1", businessId: "b1", name: "본점" }], accounts: [{ id: "a0", businessId: "b1", bank: "국민", name: "기존", accountNo: "111-222-3333" }], staff: [{ name: "직원" }], integrations: [{ service: "x" }] } };
const ok = X.buildAccountSettings(fresh, { bank: " 신한 ", name: "매출입금", accountNo: "110-123-456789" }, "b1", id);
const err = (f, i, b) => { try { X.buildAccountSettings(f, i, b, id); return ""; } catch (e) { return e.message; } };
const good = { bank: "신한", name: "a", accountNo: "110-123-456789" };
globalThis.__out = { ok,
  empty: err({ settings: { businesses: [] } }, good, "b1"),
  missingBiz: err(fresh, good, "b9"),
  dup: err(fresh, { bank: "국민", name: "x", accountNo: "1112223333" }, "b1"),
  badNo: err(fresh, { bank: "국민", name: "x", accountNo: "12ab" }, "b1"),
  blank: err(fresh, { bank: "", name: "x", accountNo: "123456" }, "b1"),
  mask: [X.maskAccount("110-123-456789"), X.maskAccount(""), X.maskAccount("12")] };
""")
    _assert_no_legacy(result)
    out = result["out"]
    s = out["ok"]["settings"]
    assert out["ok"]["account"] == {"id": "acct-test", "bank": "신한", "name": "매출입금", "accountNo": "110-123-456789", "businessId": "b1"}
    assert [a["id"] for a in s["accounts"]] == ["a0", "acct-test"]
    assert s["businesses"] and s["branches"] and s["staff"] and s["integrations"]  # 다른 기초값을 지우지 않는다.
    assert "저장을 멈췄습니다" in out["empty"]
    assert "사업자가 기초 설정에 없습니다" in out["missingBiz"]
    assert out["dup"] == "이미 등록된 계좌입니다."
    assert "6~30자리" in out["badNo"] and "모두 입력" in out["blank"]
    assert out["mask"] == ["••••6789", "-", "••••"]


def test_account_save_uses_existing_settings_route_with_fresh_read_and_verify():
    block = EXTRAS.split("async function saveAccount", 1)[1].split("// ── 모듈 화면 띄우기", 1)[0]
    first_get = block.index('ctx.financeApi("/settings")')
    put = block.index('method: "PUT"')
    verify = block.index('ctx.financeApi("/settings")', put)
    assert first_get < block.index("buildAccountSettings(") < put < verify
    assert "window.confirm(" in block and block.index("window.confirm(") < first_get
    assert 'ctx.financeApi("/settings", { method: "PUT"' in block


def test_view_state_order_matches_legacy_contract():
    result = _extras("""
const X = window.ObysAdminExtras; const env = { login() {}, retry() {} };
const k = ctx => { const c = X.viewStateConfig("salesLedger", ctx, env); return [c.kind, c.title]; };
globalThis.__out = {
  expired: k({ error: { status: 401 }, pendingReview: 3 }),
  forbidden: k({ error: { status: 403 } }),
  error: k({ error: { status: 500, message: "boom" } }),
  pending: k({ pendingReview: 2, connected: false }),
  unlinked: k({ connected: false, source: "은행 계좌" }),
  period: k({ periodFiltered: true }),
  custom: k({ emptyTitle: "등록된 사업자가 없습니다." }),
  base: k({})
};
""")
    out = result["out"]
    assert out["expired"][0] == "expired" and out["forbidden"][0] == "forbidden" and out["error"][0] == "error"
    assert out["pending"][1] == "확정된 거래가 아직 없습니다."
    assert out["unlinked"][1] == "은행 계좌 연결이 필요합니다."
    assert out["period"][1] == "선택한 기간에 거래가 없습니다."
    assert out["custom"][1] == "등록된 사업자가 없습니다." and out["base"][1] == "자료가 없습니다"


# ── admin.html 배선 ─────────────────────────────────────────

def test_load_live_route_skips_workspace_api_for_moved_screens():
    lines = {p: next(s.strip() for s in ADMIN.splitlines() if s.strip().startswith(p)) for p in (
        "const MANAGER_READ_ROUTES=", "function canReadRouteV41(", "async function loadLiveRoute(")}
    code = "const assert=require('node:assert/strict');const state={readRole:'owner',route:'month-close',businessId:'b1',requestId:4};" \
        "const history={replaceState(){}};let rendered=0,paths=[];function renderPageV41(){rendered+=1}" \
        "async function workspaceApi(p){paths.push(p);return {records:[]}}" \
        "globalThis.ObysAdminExtras={has:r=>r==='month-close'};" + "\n".join(lines.values()) + \
        "\nloadLiveRoute().then(()=>{assert.equal(rendered,1);assert.equal(paths.length,0);assert.equal(state.requestId,5);" \
        "assert.equal(state.route,'month-close');state.readRole='member';state.route='basic-settings';" \
        "globalThis.ObysAdminExtras={has:()=>true};return loadLiveRoute()}).then(()=>{assert.equal(state.route,'home');console.log('ok')})" \
        ".catch(e=>{console.error(e);process.exitCode=1})"
    assert _node(code).strip() == "ok"


def test_render_page_dispatches_moved_screens_before_generic_tables():
    render = ADMIN.split("function renderPageV41(){", 1)[1]
    assert render.index("canReadRouteV41(state.route)") < render.index("extraV41.render(state.route,page,extraContextV41())")
    assert render.index("extraV41.render(") < render.index("renderPeopleV41(c)")
    assert 'pageLabel").textContent=extraV41?extraV41.title(state.route):c.title' in render
    ctx = next(s.strip() for s in ADMIN.splitlines() if s.strip().startswith("function extraContextV41("))
    for key in ("businessId:state.businessId", "role:state.readRole", "financeApi:financeApiV41", "goRoute:goRouteV41", "onExpired:clearAuthAndOpenLogin"):
        assert key in ctx
    assert 'state.route=configs[initial]||extraRouteV41(initial)?initial:"home";' in ADMIN
    assert "if(configs[state.route]?.register)openRegisterV41();" in ADMIN
