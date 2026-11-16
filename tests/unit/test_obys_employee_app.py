"""OBYS-V41-EMPLOYEE-APP-20261116 — 직원 화면을 새 화면(employee.html)으로 이전.

대표 지시(2026-11-16): 레거시(index.html) 직원 화면은 쓰지 않는다. 직원 화면을 새 화면으로 옮긴다.
- employee.html: 내 계약서(보기·서명, modules/employee-portal.js 재사용)·입사서류·급여명세·근태 달력·정정 요청·출퇴근(clock.html).
- 진입: admin.html 의 직원 분기 2곳과 index.html 직원 분기 → employee.html. 관리자 '직원 화면 보기' → employee.html?preview=1(읽기 전용).
- 데이터는 기존 서버 API 만 쓴다. 레거시 전역 변수·함수 의존 0건.
동작 검증은 modules/employee-app.js 를 node vm 에서 가짜 DOM·가짜 fetch 로 돌린다(운영 API·DB 접속 없음).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OBYS = ROOT / "app" / "static" / "apps" / "obys"
EMPLOYEE_HTML = (OBYS / "employee.html").read_text(encoding="utf-8")
APP_JS_PATH = OBYS / "modules" / "employee-app.js"
APP_JS = APP_JS_PATH.read_text(encoding="utf-8")
PORTAL_JS_PATH = OBYS / "modules" / "employee-portal.js"
PORTAL_JS = PORTAL_JS_PATH.read_text(encoding="utf-8")
ADMIN = (OBYS / "admin.html").read_text(encoding="utf-8")
INDEX = (OBYS / "index.html").read_text(encoding="utf-8")
FINANCE_API = (ROOT / "app" / "api" / "obys_finance.py").read_text(encoding="utf-8")
WORKSPACE_API = (ROOT / "app" / "api" / "obys_workspaces.py").read_text(encoding="utf-8")
NODE = shutil.which("node")

# index.html 레거시 화면의 전역 상태·함수. 새 화면(과 재사용 모듈)은 이것을 하나도 쓰지 않는다.
LEGACY_GLOBALS = (
    "authSession", "employeeJoinRequests", "visibleContracts", "visiblePayrollStatements", "visibleOnboardingDocuments",
    "isCurrentEmployeeRecord", "currentEmployeeEmail", "currentEmployeeName", "employeePreview ", "employeePreview.",
    "renderAll(", "saveState(", "setAuthMode(", "STORE_KEY", "loadEmployeeContracts", "payrollStatements",
    "onboardingDocuments", "downloadSignedContractPdf", "submitContractSignature", "els.", "window.opener", "parent.",
    "employee_preview=1",
)


def _code_only(text: str) -> str:
    """주석을 지운 코드(/* */ 와 // 줄 주석). 문자열 안의 // (URL) 는 남긴다."""
    # "image/*" 같은 문자열 속 /* 는 주석이 아니다 — 줄 처음·공백·{ ; 뒤의 /* 만 주석으로 본다.
    text = re.sub(r"(^|[\s{;])/\*.*?\*/", r"\1", text, flags=re.S | re.M)
    return re.sub(r"(^|[\s;{}])//[^\n]*", r"\1", text)


def _run_node(script: str, tmp_path: Path, env: dict | None = None) -> str:
    path = tmp_path / "harness.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(path)], text=True, capture_output=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **(env or {})},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _routes(source: str, prefix: str = "") -> set[str]:
    found = set()
    for method, path in re.findall(r'@router\.(get|post|put|patch|delete)\("([^"]+)"', source):
        found.add((method.upper(), prefix + re.sub(r"\{[^}]+\}", "{}", path)))
    return found


class TestStaticWiring:
    def test_employee_html_loads_reused_modules_only(self):
        scripts = re.findall(r'<script src="([^"]+)"', EMPLOYEE_HTML)
        names = [Path(src.split("?")[0]).name for src in scripts]
        assert names == ["company-seal.js", "contract-core.js", "employee-portal.js", "attendance-corrections.js",
                         "employee-app.js"]
        assert 'href="/static/apps/obys/modules/employee-portal.css?v=' in EMPLOYEE_HTML
        for el in ('id="employeePortal" class="ep-root hidden"', 'id="emSection"', 'id="emRecovery"', 'id="emAlert"',
                   'id="emPreviewBanner"', 'id="emPicker"', 'data-em-preview-exit'):
            assert el in EMPLOYEE_HTML, el
        assert "window.obysEmployeeApp.createApp(" in EMPLOYEE_HTML

    def test_install_ping_not_loaded_in_read_only_preview(self):
        # 리뷰 지적(runner-5cc12cf7 P1): 접속 기록 POST(/app-install/ping)는 미리보기에서 보내지 않는다.
        # 수집 모듈은 정적 태그로 넣지 않고, preview 가 아닐 때만 동적으로 불러온다.
        assert not re.search(r'<script[^>]*app-install-ping\.js', EMPLOYEE_HTML)
        inline = EMPLOYEE_HTML[EMPLOYEE_HTML.index('var preview = '):]
        guard = inline[inline.index("if (!preview) {"):]
        guard = guard[:guard.index("}") + 1]
        assert "app-install-ping.js" in guard
        assert 'setAttribute("data-business-keys", "obys_clock_business_id,obys_v41_business_id")' in guard
        assert inline.count("app-install-ping.js") == 1
        assert 'var preview = new URLSearchParams(window.location.search).get("preview") === "1";' in EMPLOYEE_HTML

    def test_legacy_dependency_is_zero(self):
        # 새 화면·새 모듈·재사용 포털 모듈의 코드(주석 제외)에 레거시 전역이 없다.
        for name, text in (("employee.html", EMPLOYEE_HTML), ("employee-app.js", APP_JS), ("employee-portal.js", PORTAL_JS)):
            code = _code_only(text)
            for needle in LEGACY_GLOBALS:
                assert needle not in code, f"{name}: {needle}"
        # index.html 주소는 로그인 화면으로 보낼 때 한 번만 쓴다.
        code = _code_only(APP_JS)
        assert re.findall(r"/static/apps/obys/index\.html[^\"']*", code) == ["/static/apps/obys/index.html"]
        assert 'login: "/static/apps/obys/index.html"' in code
        assert "index.html" not in _code_only(EMPLOYEE_HTML)

    def test_v41_design_system_and_touch_targets(self):
        # admin.html(V4.1) 과 같은 색 체계.
        for token in ("--line:#e2e8f0", "--ink:#172033", "--muted:#667085", "--blue:#2457d6", "--bg:#f4f6f9"):
            assert token in EMPLOYEE_HTML and token in ADMIN, token
        assert re.search(r"\.ep-btn,\.ep-pc-nav button,\.ep-link,\.em-btn,\.em-field,\.em-pick,\.em-day,\.em-details summary\{min-height:44px\}", EMPLOYEE_HTML)
        assert "@media(max-width:760px)" in EMPLOYEE_HTML
        assert 'name="viewport" content="width=device-width,initial-scale=1' in EMPLOYEE_HTML

    def test_no_mock_or_hardcoded_data(self):
        code = _code_only(APP_JS) + _code_only(EMPLOYEE_HTML)
        for needle in ("mockup", "목업", "sample", "dummy", "홍길동", "@example", "biz-"):
            assert needle not in code, needle

    def test_only_existing_server_routes_are_called(self):
        finance = _routes(FINANCE_API, "/api/v1/yeoljeong-finance")
        workspace = _routes(WORKSPACE_API, "/api/v1/workspaces")
        code = _code_only(APP_JS)
        called = set()
        for helper, base in (("financeApi", "/api/v1/yeoljeong-finance"), ("workspaceApi", "/api/v1/workspaces"), ("authApi", "/api/v1/auth")):
            for raw, rest in re.findall(rf'{helper}\(\s*[`"]([^`"?]+)(?:\?[^`"]*)?[`"]([^)]*)\)', code):
                path = base + re.sub(r"\$\{[^}]+\}", "{}", raw)
                method = re.search(r'method:\s*"([A-Z]+)"', rest)
                called.add((method.group(1) if method else "GET", path))
        for raw in re.findall(r"`\$\{FINANCE_BASE\}([^`?]+)`", code):
            if raw == "${path}":
                continue  # financeApi 자체 정의
            called.add(("GET", "/api/v1/yeoljeong-finance" + re.sub(r"\$\{[^}]+\}", "{}", raw)))
        assert called, "API 호출을 찾지 못했다"
        for method, path in sorted(called):
            if path.startswith("/api/v1/auth"):
                assert path == "/api/v1/auth/tenants/{}/switch" and method == "POST"
                continue
            assert (method, path) in finance | workspace, (method, path)
        # 직원 화면이 쓰는 핵심 API 가 모두 있다.
        for needed in (("GET", "/api/v1/yeoljeong-finance/session"), ("GET", "/api/v1/yeoljeong-finance/contracts"),
                       ("GET", "/api/v1/yeoljeong-finance/payroll"), ("GET", "/api/v1/yeoljeong-finance/onboarding/documents"),
                       ("POST", "/api/v1/yeoljeong-finance/onboarding/documents"), ("GET", "/api/v1/workspaces/{}/attendance/records"),
                       ("GET", "/api/v1/yeoljeong-finance/payroll/{}/pdf"), ("GET", "/api/v1/yeoljeong-finance/contracts/{}/signed-pdf")):
            assert needed in called, needed

    def test_clock_and_corrections_are_reused(self):
        assert 'clock: "/static/apps/obys/clock.html"' in APP_JS
        assert "corrections.mount(fix, { api: workspaceApi" in APP_JS
        assert "corrections.mountSelfEntry?.(self, { api: workspaceApi" in APP_JS


class TestEntryRoutes:
    def test_admin_employee_branches_go_to_employee_app(self):
        leave = ADMIN[ADMIN.index("async function leaveForEmployeeEntryV41"):ADMIN.index("async function startWorkspaceV41")]
        assert leave.count('location.replace("/static/apps/obys/employee.html")') == 2
        assert "index.html" not in leave

    def test_admin_preview_opens_employee_app_read_only(self):
        opener = next(line for line in ADMIN.splitlines() if "function openEmployeePreviewV41" in line)
        assert 'window.location.href="/static/apps/obys/employee.html?preview=1"' in opener
        assert "employee_preview=1" not in opener

    def test_index_employee_branch_goes_to_employee_app(self):
        assert 'const EMPLOYEE_APP_PATH = "/static/apps/obys/employee.html";' in INDEX
        verify = INDEX[INDEX.index("    async function verifyLegacyEntry()"):INDEX.index("    async function init()")]
        redirect = verify.index("if (shouldOpenEmployeeApp(session)) return !openEmployeeApp();")
        assert verify.index("followEntryTenantRedirect(session)") < redirect < verify.index("isEmployeeAccount()) return true;")
        # 판정은 서버 /session 응답만 본다(로컬 가입 기록과 섞지 않는다 → employee.html 판정과 어긋나지 않는다).
        should = INDEX[INDEX.index("    function shouldOpenEmployeeApp(session)"):INDEX.index("    function openEmployeeApp()")]
        assert "session?.permissions" in should and "authPermissions()" not in should and "isEmployeeAccount()" not in should


CORE_CHECKS = r"""
const assert = require('node:assert/strict');
const { core } = require(process.env.APP_JS);

// 진입 판정 — admin.html leaveForEmployeeEntryV41 과 같은 기준
assert.equal(core.decideEntry({ permissions: { role: 'employee' } }), 'employee');
assert.equal(core.decideEntry({ permissions: { role: 'employee_pending', employee_request_status: 'pending' } }), 'employee');
assert.equal(core.decideEntry({ permissions: { role: 'member', employee_request_status: 'approved' } }), 'employee');
assert.equal(core.decideEntry({ permissions: { role: 'store_manager', employee_request_status: 'approved', can_manage_onboarding: true } }), 'admin');
assert.equal(core.decideEntry({ permissions: { role: 'owner', can_manage_onboarding: true } }), 'admin');
assert.equal(core.decideEntry({ entry_tenant_redirect: { tenant_id: 't2' }, permissions: { role: 'owner' } }), 'switch_tenant');
assert.equal(core.decideEntry({ permissions: { role: 'owner', can_manage_onboarding: true } }, { preview: true }), 'preview');
assert.equal(core.decideEntry({ permissions: { role: 'employee' } }, { preview: true }), 'forbidden_preview');
assert.equal(core.decideEntry({ permissions: { role: 'member' } }, { preview: true }), 'forbidden_preview');

// 미리보기 1회성 요청: 읽는 즉시 삭제, 5분 초과·형식 오류 무시, 마스킹 이메일 버림, 되돌아갈 주소 검증
const mem = () => { const m = new Map(); return { m, getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) }; };
let s = mem();
const now = 1_800_000_000_000;
s.setItem(core.PREVIEW_REQUEST_KEY, JSON.stringify({ email: 'Emp@X.kr', name: '직원A', returnTo: 'v41', returnHash: '#payroll', createdAt: now - 1000 }));
let r = core.consumePreviewRequest(s, now);
assert.deepEqual(r, { open: 'employee', target: { email: 'emp@x.kr', name: '직원A', requestId: '' }, returnHash: '#payroll' });
assert.equal(s.m.has(core.PREVIEW_REQUEST_KEY), false, '읽은 뒤 삭제');
assert.equal(core.consumePreviewRequest(s, now), null, '두 번째는 없음');
s.setItem(core.PREVIEW_REQUEST_KEY, JSON.stringify({ email: 'e@x.kr', createdAt: now - 6 * 60 * 1000 }));
assert.equal(core.consumePreviewRequest(s, now), null, '5분 초과');
assert.equal(s.m.has(core.PREVIEW_REQUEST_KEY), false);
s.setItem(core.PREVIEW_REQUEST_KEY, '{broken');
assert.equal(core.consumePreviewRequest(s, now), null);
s.setItem(core.PREVIEW_REQUEST_KEY, JSON.stringify({ open: 'picker', returnTo: 'v41', returnHash: 'javascript:alert(1)', createdAt: now }));
assert.deepEqual(core.consumePreviewRequest(s, now), { open: 'picker', returnHash: '#employees' });
s.setItem(core.PREVIEW_REQUEST_KEY, JSON.stringify({ email: 'em***@x.kr', name: '가', createdAt: now }));
assert.equal(core.consumePreviewRequest(s, now).open, 'picker', '마스킹 이메일만 있으면 선택창');

// 미리보기 범위: 한 직원 몫만
const target = { email: 'a@x.kr', requestId: 'r1' };
const rows = [
  { id: 1, employee_email: 'a@x.kr', employee_request_id: 'r1' },
  { id: 2, employee_email: 'a@x.kr', employee_request_id: 'r2' },  // 같은 이메일 다른 근무지
  { id: 3, employee_email: 'b@x.kr' },
  { id: 4, employee_email: 'A@X.KR' },
  { id: 5 },
];
assert.deepEqual(core.scopeToTarget(rows, target).map(x => x.id), [1, 4]);
assert.deepEqual(core.scopeToTarget(rows, { email: 'a@x.kr', requestId: '' }).map(x => x.id), [1, 2, 4]);
assert.deepEqual(core.scopeToTarget(rows, { email: '', requestId: '' }), []);

// 급여: 미리보기는 확정·지급만, 대체된 이전 버전 제외, 최근 월 먼저
const pay = [
  { id: 'd', employee_email: 'a@x.kr', status: 'draft', payroll_month: '2026-10' },
  { id: 'c', employee_email: 'a@x.kr', status: 'confirmed', payroll_month: '2026-09' },
  { id: 'p', employee_email: 'a@x.kr', status: 'paid', payroll_month: '2026-10', totals: { gross_pay: 2000000, total_deductions: 180000, net_pay: 1820000 } },
  { id: 'old', employee_email: 'a@x.kr', status: 'paid', payroll_month: '2026-08', superseded_by: 'x' },
  { id: 'b', employee_email: 'b@x.kr', status: 'paid', payroll_month: '2026-10' },
];
assert.deepEqual(core.visiblePayroll(pay, { preview: true, target: { email: 'a@x.kr' } }).map(x => x.id), ['p', 'c']);
assert.deepEqual(core.visiblePayroll(pay.slice(1, 3)).map(x => x.id), ['p', 'c'], '직원 본인 응답은 서버가 거른 그대로');
const card = core.payrollCard(pay[2]);
assert.equal(card.title, '2026년 10월 급여'); assert.equal(card.net, '1,820,000원'); assert.deepEqual(card.status, ['ok', '지급 완료']);

// 입사서류 상태
const types = [{ type: 'id_card', label: '신분증', requirement: '필수' }, { type: 'bankbook', label: '통장사본', requirement: '필수' }, { type: 'etc', label: '기타', requirement: '선택' }, { type: 'health_certificate', label: '보건증', requirement: '필수' }];
const docs = [
  { id: 'd1', document_type: 'id_card', status: 'approved', uploaded_at: '2026-11-01' },
  { id: 'd2', document_type: 'bankbook', status: 'rejected', review_memo: '흐림', uploaded_at: '2026-11-01' },
  { id: 'd3', document_type: 'health_certificate', status: 'missing', missing_document: true, is_placeholder: true },
];
const list = core.onboardingChecklist(types, docs);
assert.deepEqual(list.map(x => [x.type, x.state]), [['id_card', 'approved'], ['bankbook', 'resubmit'], ['health_certificate', 'missing']]);
assert.equal(list[1].memo, '흐림');
assert.deepEqual(core.onboardingProgress(list), { done: 1, total: 3 });
assert.equal(core.suggestExpiry('health_certificate', '2026-03-01'), '2027-02-28');
assert.equal(core.suggestExpiry('id_card', '2026-03-01'), '');
assert.match(core.validateUpload({ documentType: 'health_certificate', file: { size: 10 }, employeeEmail: 'a@x.kr' }), /만료일/);
assert.match(core.validateUpload({ documentType: 'id_card', file: { size: 16 * 1024 * 1024 }, employeeEmail: 'a@x.kr' }), /15MB/);
assert.match(core.validateUpload({ documentType: 'id_card', employeeEmail: 'a@x.kr' }), /파일/);
assert.equal(core.validateUpload({ documentType: 'id_card', file: { size: 10 }, employeeEmail: 'a@x.kr' }), '');

// 근태 달력
const grid = core.monthGrid('2026-11');
assert.equal(grid.length, 5); assert.equal(grid[0][0], '2026-11-01'); assert.equal(grid[4][1], '2026-11-30'); assert.equal(grid[4][2], null);
assert.equal(core.monthGrid('2026-02')[0].indexOf('2026-02-01'), 0);
assert.equal(core.shiftMonth('2026-01', -1), '2025-12'); assert.equal(core.shiftMonth('2026-12', 1), '2027-01');
assert.deepEqual(core.monthBounds('2028-02'), { from: '2028-02-01', to: '2028-02-29', days: 29 });
const entries = [
  core.attendanceEntry({ id: 'a', raw: { work_date: '2026-11-03', start_at: '09:00:00', end_at: '18:00:00', break_minutes: 60, status: 'approved' } }),
  core.attendanceEntry({ id: 'b', raw: { work_date: '2026-11-04', start_at: '22:00', end_at: '02:30', break_minutes: 30, status: 'pending' } }),
  core.attendanceEntry({ id: 'c', raw: { work_date: '2026-11-05', start_at: '09:00', end_at: '10:00', status: 'rejected' } }),
];
assert.equal(entries[1].worked, 240, '자정 넘김');
assert.deepEqual(core.attendanceSummary(entries, '2026-11'), { days: 2, minutes: 720, hoursText: '12시간', pending: 1 });
assert.deepEqual(Object.keys(core.attendanceByDay(entries)).sort(), ['2026-11-03', '2026-11-04', '2026-11-05']);

// 복구 안내 — 다음 행동이 있다
assert.deepEqual(core.recoveryFor({ status: 401 }).actions, ['login', 'retry']);
assert.equal(core.recoveryFor({ status: 403, message: '권한 없음' }).kind, 'forbidden');
assert.match(core.recoveryFor({ status: 403 }).message, /관리자/);
assert.deepEqual(core.recoveryFor({ status: 0, network: true }).actions, ['retry']);
assert.equal(core.recoveryFor({ status: 500, message: 'x' }).kind, 'error');

// 읽기 전용 잠금
assert.throws(() => core.assertWritable('POST', true), e => e.code === 'EMPLOYEE_PREVIEW_READ_ONLY');
assert.throws(() => core.assertWritable('delete', true), e => e.code === 'EMPLOYEE_PREVIEW_READ_ONLY');
core.assertWritable('GET', true); core.assertWritable('POST', false);
assert.equal(core.loginUrl('a/b'), '/static/apps/obys/index.html?yf_contract_token=a%2Fb');
console.log('CORE_OK');
"""


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_core_rules_in_node(tmp_path):
    assert "CORE_OK" in _run_node(CORE_CHECKS, tmp_path, {"APP_JS": str(APP_JS_PATH)})


# 가짜 DOM·가짜 fetch 로 createApp 을 실제로 돌린다. 포털은 sync/openContract 호출만 기록한다.
APP_HARNESS = r"""
const assert = require('node:assert/strict');
const { createApp } = require(process.env.APP_JS);

function el(id) {
  const classes = new Set();
  return {
    id, innerHTML: '', textContent: '', hidden: false, isConnected: true, children: [], _filtered: null,
    classList: { toggle: (c, on) => { if (on === undefined ? !classes.has(c) : on) classes.add(c); else classes.delete(c); }, contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c) },
    appendChild(child) { this.children.push(child); return child; }, querySelector: () => null, querySelectorAll: () => [],
  };
}

function setup(opts) {
  const els = {};
  const listeners = {};
  const calls = [];
  const document = {
    cookie: '', body: el('body'),
    getElementById: id => (els[id] = els[id] || el(id)),
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    querySelector: () => null, createElement: tag => el(tag),
  };
  const location = { search: opts.search || '', protocol: 'https:', replaced: null, replace(url) { this.replaced = url; }, reload() { this.reloaded = true; } };
  const mem = init => { const m = new Map(Object.entries(init || {})); return { m, getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) }; };
  const window = { location, history: { replaceState: (a, b, url) => { window.replacedUrl = url; } }, URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} } };
  const fetch = async (url, init = {}) => {
    const method = String(init.method || 'GET').toUpperCase();
    calls.push({ url, method, auth: init.headers?.Authorization || '' });
    const reply = opts.routes(url, method);
    if (reply instanceof Error) throw reply;
    const [status, body] = reply || [404, { detail: 'not found' }];
    return { ok: status >= 200 && status < 300, status, json: async () => body, blob: async () => ({}), headers: { get: () => '' } };
  };
  const portal = { syncs: [], opened: null, sync(b) { this.syncs.push({ active: b.active, readOnly: b.readOnly, count: b.contracts.length }); }, openContract(c, bundle, o) { this.opened = { c, o }; } };
  const app = createApp({
    document, window, fetch, portal,
    sessionStorage: mem(opts.session), localStorage: mem(opts.token === false ? {} : { aads_token: 'tok' }),
    contractCore: { contractTypeLabels: { part_time: '단시간' }, contractPreviewHtml: c => `<p>${c.id}</p>`, setBusinessResolver() {} },
    corrections: { mounted: 0, mount() { this.mounted += 1; }, mountSelfEntry() {} },
  });
  app.bind();
  return { app, els, calls, location, portal, window, document, listeners };
}

const EMP_SESSION = { user: { email: 'emp@x.kr', name: '' }, permissions: { role: 'employee', employee_request_status: 'approved', employee_request_id: 'r1', can_manage_onboarding: false } };
const ADMIN_SESSION = { user: { email: 'boss@x.kr' }, permissions: { role: 'owner', can_manage_onboarding: true } };
const F = '/api/v1/yeoljeong-finance';
function employeeRoutes(session, extra = {}) {
  return (url, method) => {
    if (extra[`${method} ${url}`]) return extra[`${method} ${url}`];
    if (url === `${F}/session`) return [200, session];
    if (url === `${F}/contracts`) return [200, { contracts: [{ id: 'c1', status: 'requested', employee_email: 'emp@x.kr' }] }];
    if (url === `${F}/contracts/pending-signature`) return [200, { pending: [{ id: 'c1', expires_at: '2026-11-30' }] }];
    if (url === `${F}/payroll`) return [200, { statements: [{ id: 'p1', status: 'paid', payroll_month: '2026-10', employee_email: 'emp@x.kr' }] }];
    if (url === `${F}/onboarding/document-types`) return [200, { document_types: [{ type: 'id_card', label: '신분증', requirement: '필수' }] }];
    if (url === `${F}/onboarding/documents`) return [200, { documents: [] }];
    if (url === `${F}/employees/join-requests`) return [200, { requests: [{ id: 'r1', status: 'approved', name: '김직원', email: 'emp@x.kr', branch: '본점', business_id: 'b1' }] }];
    if (url === '/api/v1/workspaces/businesses') return [200, { businesses: [{ id: 'b1', name: '가게' }] }];
    if (url.startsWith('/api/v1/workspaces/b1/attendance/records')) return [200, { records: [{ id: 'a1', raw: { work_date: '2026-11-03', start_at: '09:00', end_at: '18:00', break_minutes: 60, status: 'approved' } }] }];
    return [404, { detail: 'nf' }];
  };
}

(async () => {
  // 1) 직원 진입: 본인 자료를 기존 API 로 불러오고 포털을 켠다. 쓰기 요청은 없다.
  let t = setup({ routes: employeeRoutes(EMP_SESSION) });
  await t.app.start();
  assert.equal(t.app.app.mode, 'employee');
  assert.equal(t.app.app.ready, true);
  assert.equal(t.location.replaced, null);
  assert.equal(t.app.bridge.readOnly, false);
  assert.equal(t.app.bridge.employeeName, '김직원');
  assert.equal(t.app.bridge.branch, '본점');
  assert.equal(t.app.bridge.contracts.length, 1);
  assert.equal(t.app.bridge.pendingSign.length, 1);
  assert.equal(t.portal.syncs.at(-1).active, true);
  assert.ok(t.calls.every(c => c.method === 'GET'), '시작할 때 쓰기 없음');
  assert.ok(t.calls.every(c => c.auth === 'Bearer tok'));
  assert.equal(t.els.emLoading.hidden, true);
  // 급여·근태·서류 섹션
  t.app.openSection('payroll');
  assert.match(t.els.emSection.innerHTML, /2026년 10월 급여/);
  assert.match(t.els.emSection.innerHTML, /data-em-payroll-pdf="p1"/);
  t.app.openSection('attendance');
  await new Promise(r => setTimeout(r, 0));
  await new Promise(r => setTimeout(r, 0));
  t.app.app.month = '2026-11'; t.app.renderSection();
  assert.match(t.els.emSection.innerHTML, /data-em-day="2026-11-03"/);
  assert.match(t.els.emSection.innerHTML, /href="\/static\/apps\/obys\/clock.html"/);
  assert.match(t.els.emSection.innerHTML, /id="emCorrections"/);
  t.app.openSection('onboarding');
  assert.match(t.els.emSection.innerHTML, /data-em-upload/);
  assert.match(t.els.emSection.innerHTML, /data-em-doc-start="id_card"/);

  // 2) 관리자(직원 아님)가 employee.html 을 열면 관리자 화면으로 보낸다.
  t = setup({ routes: employeeRoutes(ADMIN_SESSION) });
  await t.app.start();
  assert.equal(t.location.replaced, '/static/apps/obys/admin.html');
  assert.equal(t.app.app.ready, false);

  // 3) 로그인 없음 / 세션 만료 / 권한 부족 / 네트워크 실패 → 다음 행동이 있는 복구 화면
  t = setup({ token: false, routes: employeeRoutes(EMP_SESSION) });
  await t.app.start();
  assert.equal(t.calls.length, 0);
  assert.equal(t.els.emRecovery.hidden, false);
  assert.match(t.els.emRecovery.innerHTML, /data-em-recover="login"/);
  for (const [reply, title, actions] of [
    [[401, { detail: 'expired' }], '로그인이 만료되었습니다', ['login', 'retry']],
    [[403, { detail: '현재 테넌트의 활성 멤버십이 필요합니다' }], '이 화면을 볼 권한이 없습니다', ['login', 'retry']],
    [new TypeError('Failed to fetch'), '연결을 확인해 주세요', ['retry']],
  ]) {
    t = setup({ routes: (url, m) => (url === `${F}/session` ? reply : [404, {}]) });
    await t.app.start();
    assert.equal(t.els.emRecovery.hidden, false);
    assert.ok(t.els.emRecovery.innerHTML.includes(title), title);
    for (const a of actions) assert.ok(t.els.emRecovery.innerHTML.includes(`data-em-recover="${a}"`), a);
    assert.equal(t.app.app.ready, false);
  }
  // 사용 중 일부 실패: 급여만 실패해도 나머지는 보이고, 급여 칸에 재시도가 있다.
  t = setup({ routes: employeeRoutes(EMP_SESSION, { [`GET ${F}/payroll`]: [500, { detail: 'db' }] }) });
  await t.app.start();
  assert.equal(t.app.app.ready, true);
  t.app.openSection('payroll');
  assert.match(t.els.emSection.innerHTML, /data-em-recover="retry"/);
  // 사용 중 세션 만료: 화면은 그대로, 위에 다시 로그인 안내
  t.app.bridge.handleExpiredServerSession();
  assert.equal(t.els.emAlert.hidden, false);
  assert.match(t.els.emAlert.innerHTML, /data-em-recover="login"/);

  // 4) 서명 링크 토큰: 그 계약서를 포털로 연다(토큰을 넘긴다).
  t = setup({ search: '?yf_contract_token=tk1', routes: employeeRoutes(EMP_SESSION, { [`GET ${F}/contracts/signing/tk1`]: [200, { contract: { id: 'c9', status: 'requested' }, bundle: [] }] }) });
  await t.app.start();
  assert.equal(t.portal.opened.c.id, 'c9');
  assert.deepEqual(t.portal.opened.o, { token: 'tk1' });
  assert.equal(t.window.replacedUrl, '/static/apps/obys/employee.html');

  // 5) 관리자 미리보기: 1회성 요청 소비, 한 직원 몫만, 읽기 전용(쓰기는 보내기 전에 막힘)
  const adminRoutes = (url, method) => {
    if (url === `${F}/session`) return [200, ADMIN_SESSION];
    if (url === `${F}/employees/join-requests`) return [200, { requests: [
      { id: 'r1', status: 'approved', name: '김직원', email: 'emp@x.kr', branch: '본점', business_id: 'b1' },
      { id: 'r2', status: 'approved', name: '이직원', email: 'other@x.kr', branch: '2호점', business_id: 'b1' },
      { id: 'r3', status: 'pending', name: '대기', email: 'wait@x.kr' }] }];
    if (url === `${F}/contracts`) return [200, { contracts: [{ id: 'c1', employee_email: 'emp@x.kr', employee_request_id: 'r1', status: 'signed' }, { id: 'c2', employee_email: 'other@x.kr', status: 'requested' }] }];
    if (url === `${F}/payroll`) return [200, { statements: [{ id: 'p1', employee_email: 'emp@x.kr', status: 'draft' }, { id: 'p2', employee_email: 'emp@x.kr', status: 'confirmed', payroll_month: '2026-10' }, { id: 'p3', employee_email: 'other@x.kr', status: 'paid' }] }];
    if (url === `${F}/onboarding/document-types`) return [200, { document_types: [] }];
    if (url === `${F}/onboarding/documents`) return [200, { documents: [{ id: 'd1', employee_email: 'emp@x.kr', document_type: 'id_card' }, { id: 'd2', employee_email: 'other@x.kr', document_type: 'id_card' }] }];
    if (url === '/api/v1/workspaces/businesses') return [200, { businesses: [{ id: 'b1', name: '가게' }] }];
    if (url.startsWith('/api/v1/workspaces/b1/attendance/records')) return [200, { records: [] }];
    return [404, {}];
  };
  const request = JSON.stringify({ email: 'emp@x.kr', name: '김직원', returnTo: 'v41', returnHash: '#payroll', createdAt: Date.now() });
  t = setup({ search: '?preview=1', session: { 'obys.employeePreviewRequest': request }, routes: adminRoutes });
  await t.app.start();
  assert.equal(t.app.app.mode, 'preview');
  assert.equal(t.app.bridge.readOnly, true);
  assert.equal(t.app.app.ready, true);
  assert.deepEqual(t.app.bridge.contracts.map(c => c.id), ['c1'], '다른 직원 계약 안 보임');
  assert.deepEqual(t.app.bridge.payroll.map(p => p.id), ['p2'], '작성중·다른 직원 급여 안 보임');
  assert.deepEqual(t.app.bridge.onboardingDocs.map(d => d.id), ['d1']);
  assert.equal(t.app.bridge.pendingSign.length, 0);
  assert.ok(!t.calls.some(c => c.url.endsWith('/contracts/pending-signature')), '미리보기는 서명 대기 조회 안 함');
  assert.equal(t.els.emPreviewBanner.hidden, false);
  assert.match(t.els.emPreviewText.textContent, /김직원 님 화면을 보는 중입니다 \(읽기 전용\)/);
  assert.equal(t.document.body.classList.contains('employee-preview'), true);
  const before = t.calls.length;
  await assert.rejects(t.app.financeApi('/contracts/signing', { method: 'POST', body: '{}' }), e => e.code === 'EMPLOYEE_PREVIEW_READ_ONLY');
  await assert.rejects(t.app.financeApi('/onboarding/documents', { method: 'POST', body: {} }), e => e.code === 'EMPLOYEE_PREVIEW_READ_ONLY');
  await assert.rejects(t.app.workspaceApi('/b1/attendance/corrections', { method: 'POST', body: '{}' }), e => e.code === 'EMPLOYEE_PREVIEW_READ_ONLY');
  assert.equal(t.calls.length, before, '쓰기 요청은 네트워크로 나가지 않는다');
  assert.ok(t.calls.every(c => c.method === 'GET'));
  t.app.openSection('onboarding');
  assert.doesNotMatch(t.els.emSection.innerHTML, /data-em-upload|data-em-doc-start/);
  assert.match(t.els.emSection.innerHTML, /읽기 전용 미리보기/);
  t.app.openSection('attendance');
  await new Promise(r => setTimeout(r, 0)); await new Promise(r => setTimeout(r, 0));
  t.app.renderSection();
  assert.doesNotMatch(t.els.emSection.innerHTML, /clock\.html|id="emCorrections"/);
  assert.ok(t.calls.some(c => c.url.includes('/attendance/records?') && c.url.includes('employee_email=emp%40x.kr')), '근태는 서버가 직원별로 거른다');
  // 되돌아가기
  t.listeners.click[0]({ target: { closest: () => ({ dataset: { emPreviewExit: '' }, disabled: false }) } });
  assert.equal(t.location.replaced, '/static/apps/obys/admin.html#payroll');

  // 6) 미리보기 요청 없이 열면 선택창(승인 직원만)
  t = setup({ search: '?preview=1', routes: adminRoutes });
  await t.app.start();
  assert.equal(t.app.app.ready, false);
  assert.equal(t.els.emPicker.hidden, false);
  assert.match(t.els.emPicker.innerHTML, /김직원/);
  assert.doesNotMatch(t.els.emPicker.innerHTML, /대기/);
  assert.doesNotMatch(t.els.emPicker.innerHTML, /emp@x\.kr/, '선택창 이메일은 가린다');

  // 7) 관리 권한 없는 계정의 ?preview=1 → 권한 부족 화면, 직원 자료 조회 없음
  t = setup({ search: '?preview=1', routes: employeeRoutes(EMP_SESSION) });
  await t.app.start();
  assert.equal(t.els.emRecovery.hidden, false);
  assert.match(t.els.emRecovery.innerHTML, /직원 화면 보기 권한이 없습니다/);
  assert.deepEqual(t.calls.map(c => c.url), [`${F}/session`]);
  console.log('APP_OK');
})().catch(error => { console.error(error); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_employee_app_flows_in_node(tmp_path):
    assert "APP_OK" in _run_node(APP_HARNESS, tmp_path, {"APP_JS": str(APP_JS_PATH)})


INDEX_HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const out = {};
for (const c of JSON.parse(process.env.CASES)) {
  const ctx = {
    URLSearchParams, String, Boolean,
    window: { location: { search: c.search || '', hash: c.hash || '', replace(url) { ctx.replaced = url; } } },
    pendingPhoneInviteToken: '', EMPLOYEE_APP_PATH: '/static/apps/obys/employee.html',
    hasServerAuth: () => c.auth !== false, readPostLoginRedirectFromUrl: () => '',
  };
  vm.createContext(ctx);
  vm.runInContext(process.env.SOURCES + '\nthis.r = shouldOpenEmployeeApp(' + JSON.stringify(c.session) + ') ? openEmployeeApp() && this.replaced : false;', ctx);
  out[c.name] = ctx.r;
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_index_employee_redirect_in_node(tmp_path):
    def src(name):
        start = INDEX.index(f"    function {name}(")
        end = INDEX.index("\n    }\n", start) + 6
        return INDEX[start:end]

    cases = [
        {"name": "employee", "session": {"permissions": {"role": "employee"}}},
        {"name": "pending", "session": {"permissions": {"role": "employee_pending", "employee_request_status": "pending"}}},
        {"name": "token", "session": {"permissions": {"role": "employee"}}, "search": "?yf_contract_token=a b"},
        {"name": "owner", "session": {"permissions": {"role": "owner", "can_manage_onboarding": True}}},
        {"name": "manager_employee", "session": {"permissions": {"role": "store_manager", "employee_request_status": "approved", "can_manage_onboarding": True}}},
        {"name": "invite", "session": {"permissions": {"role": "employee"}}, "search": "?yf_invite_token=x"},
        {"name": "legacy", "session": {"permissions": {"role": "employee"}}, "search": "?legacy=1&view=legacy"},
        {"name": "preview", "session": {"permissions": {"role": "employee"}}, "search": "?employee_preview=1"},
        {"name": "invite_hash", "session": {"permissions": {"role": "employee"}}, "hash": "#invite-x"},
        {"name": "signed_out", "session": {"permissions": {"role": "employee"}}, "auth": False},
    ]
    out = json.loads(_run_node(INDEX_HARNESS, tmp_path, {
        "SOURCES": src("shouldOpenEmployeeApp") + "\n" + src("openEmployeeApp"), "CASES": json.dumps(cases)}))
    assert out["employee"] == "/static/apps/obys/employee.html"
    assert out["pending"] == "/static/apps/obys/employee.html"
    assert out["token"] == "/static/apps/obys/employee.html?yf_contract_token=a%20b"
    for name in ("owner", "manager_employee", "invite", "legacy", "preview", "invite_hash", "signed_out"):
        assert out[name] is False, name


def test_release_assets_stamp_employee_html(tmp_path):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import obys_release_assets as tool
    finally:
        sys.path.pop(0)
    d = tmp_path / "obys"
    d.mkdir()
    (d / "employee.html").write_bytes((OBYS / "employee.html").read_bytes())
    (d / "sw.js").write_bytes((OBYS / "sw.js").read_bytes())
    tool.stamp_tree(d, "abc1234")
    stamped = (d / "employee.html").read_text(encoding="utf-8")
    for ref in ("employee-app.js", "employee-portal.js", "employee-portal.css", "contract-core.js", "attendance-corrections.js"):
        assert f"modules/{ref}?v=abc1234" in stamped, ref
    urls = tool.purge_urls(OBYS, "abc1234", "https://fb.newtalk.kr")
    assert "https://fb.newtalk.kr/static/apps/obys/employee.html" in urls
