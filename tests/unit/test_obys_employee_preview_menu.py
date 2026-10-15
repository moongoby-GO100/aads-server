"""ACCT-OBYS-EMPLOYEE-PREVIEW-MENU-ENTRY-20261015 — '직원 화면 보기'를 메뉴 항목으로 노출.

서버·DB 변경 없이 index.html(관리 화면)과 admin.html(V4.1)에 진입 메뉴를 추가한다.
- index.html: people 그룹(좌측·상단·모바일 더보기)에 관리자 전용 항목 → 직원 선택창 → 기존 startEmployeePreview.
- V4.1: 직원·급여 그룹 항목과 직원·계약서 행 버튼 → sessionStorage 1회성 키 → index.html 이 인증·권한·5분 확인 후 소비.
동작 검증은 원문 함수를 뽑아 node 로 실행한다.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
V41 = (ROOT / "admin.html").read_text(encoding="utf-8")
EDITOR = (ROOT / "modules" / "contract-editor-v41.js").read_text(encoding="utf-8")

NODE = shutil.which("node")


def _function_source(html: str, name: str, indent: str = "    ") -> str:
    found = re.search(rf"^{indent}(?:async )?function {name}\(", html, re.M)
    assert found, name
    start = found.start()
    open_at = html.index(") {", start) + 2
    depth = 0
    for pos in range(open_at, len(html)):
        char = html[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[start : pos + 1]
    raise AssertionError(name)


def _block(begin: str, end: str) -> str:
    match = re.search(rf"// {begin}\n(.*?)// {end}", INDEX, re.S)
    assert match, begin
    return match.group(1)


def _line_starting(html: str, prefix: str) -> str:
    for line in html.splitlines():
        if line.lstrip().startswith(prefix):
            return line.strip()
    raise AssertionError(prefix)


def _run_node(script_body: str, env: dict, tmp_path) -> str:
    script = tmp_path / "harness.js"
    script.write_text(script_body, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script)],
        text=True,
        capture_output=True,
        timeout=30,
        env={**env, "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


class TestIndexMenuWiring:
    def test_menu_item_exists_in_side_top_and_mobile_menus(self):
        items = re.findall(r"<button[^>]*data-employee-preview-menu[^>]*>직원 화면 보기</button>", INDEX)
        assert len(items) == 3
        # 좌측(.side-nav)·상단(#subTabs) 은 people 그룹 .tab, 모바일은 더보기 시트의 '직원' 묶음.
        tabs = [item for item in items if 'class="tab hidden"' in item]
        assert len(tabs) == 2
        assert all('data-group="people"' in item for item in tabs)
        side = INDEX.index('aria-label="좌측 주요 화면"')
        sub = INDEX.index('id="subTabs"')
        sheet = INDEX.index('id="bottomMoreSheet"')
        positions = [m.start() for m in re.finditer(r"data-employee-preview-menu", INDEX) if m.start() > side]
        assert any(side < p < sub for p in positions)
        assert any(sub < p < sheet for p in positions)
        assert any(p > sheet for p in positions)
        assert "data-employee-preview-sheet-group" in INDEX

    def test_menu_is_admin_only_and_hidden_in_employee_view_or_preview(self):
        policy = _function_source(INDEX, "applyAccessPolicy")
        assert 'button.hasAttribute("data-employee-preview-menu")' in policy
        assert "isAuthed && !employeeMode && canManageOnboarding()" in policy
        # 미리보기 중에는 canManageOnboarding() 이 거짓, 직원 계정은 권한이 없다. CSS 도 이중으로 숨긴다.
        assert "body.employee-view [data-employee-preview-menu]" in INDEX
        assert "body.employee-preview [data-employee-preview-menu]" in INDEX
        sheet_sync = _function_source(INDEX, "syncBottomTabs")
        assert "data-employee-preview-sheet-group" in sheet_sync
        assert "canManageOnboarding()" in sheet_sync and "!isEmployeeExperience()" in sheet_sync

    def test_touch_target_and_mobile_wrap(self):
        assert re.search(r"\[data-employee-preview-menu\] \{[^}]*min-height: 44px[^}]*white-space: normal", INDEX)
        assert re.search(r"\.employee-preview-pick \{[^}]*min-height: 44px[^}]*white-space: normal", INDEX)
        assert re.search(r"\.employee-preview-search \{ min-height: 44px; \}|\.employee-preview-search \{[^}]*min-height: 44px", INDEX)

    def test_picker_uses_existing_preview_path_only(self):
        block = _block("ADMIN-EMPLOYEE-PREVIEW:BEGIN", "ADMIN-EMPLOYEE-PREVIEW:END")
        assert "startEmployeePreview({ email: trigger.dataset.previewEmail" in block
        assert "/auth/admin/" not in INDEX
        assert "impersonate" not in INDEX
        assert "미리 볼 수 있는 승인된 직원이 없습니다" in block
        for read_only in ("assertEmployeePreviewReadOnly(options.method)",):
            assert read_only in INDEX
        code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("//"))
        assert "sessionStorage" not in code and "localStorage" not in code

    def test_handoff_is_consumed_after_session_refresh(self):
        refresh = _function_source(INDEX, "refreshFinanceSessionInBackground")
        assert "consumeEmployeePreviewRequest()" in refresh
        assert "dropEmployeePreviewRequest()" in refresh


class TestV41Entry:
    def test_v41_menu_item_exists_in_people_group(self):
        nav = _line_starting(V41, "function renderNav")
        assert 'g.label==="직원·급여"' in nav
        assert "data-employee-preview-entry" in nav
        assert "직원 화면 보기" in nav
        # 권한이 확인되기 전에는 숨겨 둔다.
        assert 'state.canManageOnboarding===true?"":"hidden"' in nav
        assert "min-height:44px" in V41 and "data-employee-preview-entry]" in V41

    def test_v41_navigates_to_index_without_personal_data_in_url(self):
        opener = _line_starting(V41, "function openEmployeePreviewV41")
        assert 'window.location.href="/static/apps/obys/index.html?legacy=1&view=legacy"' in opener
        # index.html 이 로그인 상태를 V4.1 로 되돌리지 않도록 legacy 진입 URL 을 쓴다.
        assert "email" not in re.search(r'window\.location\.href="([^"]*)"', opener).group(1)
        assert "encodeURIComponent" not in opener
        assert 'sessionStorage.setItem(EMPLOYEE_PREVIEW_REQUEST_KEY_V41' in opener
        assert 'returnTo:"v41"' in opener
        assert 'returnHash:window.location.hash||"#employees"' in opener
        assert 'obys.employeePreviewRequest' in V41

    def test_contract_editor_rows_use_masked_email_guard(self):
        helper = _function_source(EDITOR, "previewButton", indent="  ")
        assert 'clean.includes("*")' in helper
        assert "canManage()" in helper
        assert "previewButton(employee.email, employee.name)" in EDITOR
        assert 'previewButton(pick(contract, "employeeEmail"), pick(contract, "employeeName"))' in EDITOR
        assert "obysEmployeePreviewV41?.open({ email: button.dataset.cv41PreviewEmail" in EDITOR

    def test_no_new_server_path(self):
        assert "impersonate" not in V41 + EDITOR
        assert "/session" in _line_starting(V41, "async function loadEmployeePreviewPermissionV41")


PICKER_HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');

class FakeEl {
  constructor({ tag = 'button', attrs = {}, text = '', classes = [], id = '', parent = null } = {}) {
    this.tag = tag; this.attrMap = { ...attrs }; this.textContent = text;
    this.classes = classes; this.id = id; this.parent = parent;
    this.dataset = {}; this.title = ''; this.disabled = false;
  }
  get attributes() { return Object.keys(this.attrMap).map(name => ({ name })); }
  getAttribute(name) { return name in this.attrMap ? this.attrMap[name] : null; }
  matchesToken(token) {
    token = token.trim();
    if (token.startsWith('.')) return this.classes.includes(token.slice(1));
    if (token.startsWith('#')) return this.id === token.slice(1);
    const m = token.match(/^(\w+)?\[([\w-]+)(?:=(\w+))?\]$/);
    if (m) {
      if (m[1] && this.tag !== m[1]) return false;
      if (!(m[2] in this.attrMap)) return false;
      return m[3] === undefined || this.attrMap[m[2]] === m[3];
    }
    return this.tag === token;
  }
  closest(selector) {
    const tokens = selector.split(',');
    for (let el = this; el; el = el.parent) if (tokens.some(t => el.matchesToken(t))) return el;
    return null;
  }
}

function box(initialHidden) {
  const classes = new Set(initialHidden ? ['hidden'] : []);
  return { classes, innerHTML: '', value: '', focused: false, focus() { this.focused = true; },
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) } };
}

const listeners = {};
const toasts = [];
const calls = { renderAll: 0, setView: [], sheetClosed: 0, reloaded: 0 };
const elements = { employeePreviewPicker: box(true), employeePreviewList: box(false), employeePreviewSearch: box(false),
  employeePreviewBanner: box(true), employeePreviewBannerText: { textContent: '' } };
const bodyClasses = new Set();
const store = new Map();
let permissions = { role: 'admin', can_manage_onboarding: true };

const ctx = {
  document: {
    body: { classList: { add: c => bodyClasses.add(c), remove: c => bodyClasses.delete(c) } },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    getElementById: id => elements[id] || null,
    querySelectorAll: () => [],
  },
  window: { fetch: () => Promise.resolve({ ok: true }) },
  sessionStorage: {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: k => store.delete(k),
  },
  Promise, Error, Array, String, Boolean, Object, RegExp, JSON, Map, Set, Number, Date, console,
  requestAnimationFrame: fn => fn(),
  showToast: m => toasts.push(m),
  renderAll: () => { calls.renderAll += 1; },
  setView: v => { calls.setView.push(v); },
  toggleBottomSheet: open => { if (!open) calls.sheetClosed += 1; },
  loadEmployeeJoinRequests: () => { calls.reloaded += 1; return Promise.resolve(); },
  loadEmployeeContracts: () => { calls.reloaded += 1; return Promise.resolve(); },
  hasServerAuth: () => true,
  authPermissions: () => permissions,
  authSession: { user: { email: 'admin@example.com' } },
  loadEmployeeSelfSignupMap: () => ({}),
  escapeHtml: s => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'),
  currentView: 'employees',
  appViewNames: ['dashboard', 'employees', 'contracts', 'attendance', 'onboarding', 'payroll'],
  employeeViewNames: ['attendance', 'onboarding', 'contracts', 'payroll'],
  employeeJoinRequests: [], approvedEmployees: [], state: { contracts: [] },
  FakeEl, listeners, toasts, calls, elements, bodyClasses, store,
  setPermissions: p => { permissions = p; },
  assert,
};
vm.createContext(ctx);
vm.runInContext(process.env.SOURCES + '\n' + process.env.CHECKS, ctx);
console.log('NODE_OK');
"""

PICKER_CHECKS = r"""
function ev(target, extra = {}) {
  return { target, defaultPrevented: false, stopped: false, ...extra,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; } };
}
const picker = elements.employeePreviewPicker;
const list = elements.employeePreviewList;
const search = elements.employeePreviewSearch;
const menuItem = new FakeEl({ attrs: { 'data-employee-preview-menu': '' }, text: '직원 화면 보기', classes: ['tab'] });

// (e) 후보: 이메일 기준 중복 제거 + 마스킹 전용 항목 제외
employeeJoinRequests.push(
  { status: 'approved', email: 'Emp@Example.com', name: '홍길동', branch: '중화점' },
  { status: 'pending', email: 'pending@example.com', name: '대기자' },
  { status: 'approved', email: 'ab***@example.com', name: '마스킹이름' },
  { status: 'approved', email: 'cd***@example.com', name: '' },
);
approvedEmployees.push(
  { email: 'emp@example.com', name: '홍길동', branch: '중화점' },
  { employee_email: 'kim@example.com', employee_name: '김철수' },
);
state.contracts.push(
  { employeeEmail: 'EMP@example.com', employeeName: '홍길동' },
  { employee_email: 'ef***@example.com', employee_name: '' },
  { employee_email: 'gh***@example.com', employee_name: '김철수' },
  { employee_email: 'lee@example.com', employee_name: '' },
);
const candidates = employeePreviewCandidates();
assert.equal(JSON.stringify(candidates.map(c => c.email || `name:${c.name}`).sort()),
  JSON.stringify(['name:마스킹이름', 'emp@example.com', 'kim@example.com', 'lee@example.com'].sort()));
assert.equal(candidates.filter(c => c.email === 'emp@example.com').length, 1, '이메일 기준 중복 제거');
assert.equal(candidates.some(c => c.name === '대기자'), false, '승인 전 가입요청 제외');
assert.equal(candidates.some(c => c.email.includes('*')), false, '마스킹 이메일은 대상 이메일이 되지 않는다');
assert.equal(candidates.find(c => c.email === 'kim@example.com').name, '김철수');
assert.equal(candidates.some(c => c.name === '김철수' && !c.email), false, '같은 이름의 마스킹 항목은 이메일 항목에 합친다');
assert.equal(candidates.find(c => c.name === '마스킹이름').email, '', '이름이 있으면 이메일 없이 대상이 된다');

// (a)(b) 권한 없음 → 선택창 안 열림
setPermissions({ role: 'employee', can_manage_onboarding: false });
assert.equal(openEmployeePreviewPicker(), false);
assert.equal(picker.classes.has('hidden'), true);
assert.match(toasts.at(-1), /회사 관리자/);
setPermissions({ role: 'admin', can_manage_onboarding: true });

// 메뉴 클릭 → 선택창 열림(+모바일 시트 닫힘) → 목록 렌더, 이메일은 마스킹 표기
const menuClick = ev(menuItem);
listeners.click(menuClick);
assert.equal(menuClick.defaultPrevented && menuClick.stopped, true);
assert.equal(picker.classes.has('hidden'), false);
assert.equal(calls.sheetClosed >= 1, true);
assert.match(list.innerHTML, /data-preview-employee/);
assert.match(list.innerHTML, /data-preview-email="emp@example.com"/);
assert.match(list.innerHTML, /홍길동/);
assert.match(list.innerHTML, /em\*\*\*@example\.com/);
assert.doesNotMatch(list.innerHTML, /대기자/);
assert.equal(search.focused, true);

// 검색
search.value = '김철';
listeners.input(ev({ id: 'employeePreviewSearch' }));
assert.match(list.innerHTML, /김철수/);
assert.doesNotMatch(list.innerHTML, /홍길동/);
search.value = '없는사람';
listeners.input(ev({ id: 'employeePreviewSearch' }));
assert.match(list.innerHTML, /검색 결과가 없습니다/);
search.value = '';
listeners.input(ev({ id: 'employeePreviewSearch' }));

// 닫기 버튼 / Escape
listeners.click(ev(new FakeEl({ attrs: { 'data-employee-preview-picker-close': '' } })));
assert.equal(picker.classes.has('hidden'), true);
openEmployeePreviewPicker();
listeners.keydown({ key: 'Escape' });
assert.equal(picker.classes.has('hidden'), true);

// (b) 직원 선택 → 기존 startEmployeePreview, 선택창 닫힘, 읽기 전용 유지
openEmployeePreviewPicker();
const pick = new FakeEl({ attrs: { 'data-preview-employee': '' }, classes: ['employee-preview-pick'] });
pick.dataset.previewEmail = 'emp@example.com';
pick.dataset.previewName = '홍길동';
listeners.click(ev(pick));
assert.equal(employeePreview.email, 'emp@example.com');
assert.equal(employeePreview.name, '홍길동');
assert.equal(picker.classes.has('hidden'), true);
assert.equal(bodyClasses.has('employee-preview'), true);
assert.equal(elements.employeePreviewBannerText.textContent, '홍길동 님 화면을 보는 중입니다 (읽기 전용)');
assert.throws(() => assertEmployeePreviewReadOnly('POST'), /읽기 전용/);
assert.equal(openEmployeePreviewPicker(), false, '미리보기 중에는 선택창을 다시 열지 않는다');
stopEmployeePreview();

// 직원 0명 안내
employeeJoinRequests.length = 0; approvedEmployees.length = 0; state.contracts.length = 0;
openEmployeePreviewPicker();
assert.match(list.innerHTML, /미리 볼 수 있는 승인된 직원이 없습니다/);
assert.equal(calls.reloaded >= 1, true, '목록이 비면 읽기 전용 조회를 한 번 더 한다');
closeEmployeePreviewPicker();

// (d) 핸드오프: 1회 소비
const KEY = 'obys.employeePreviewRequest';
const put = obj => store.set(KEY, typeof obj === 'string' ? obj : JSON.stringify(obj));
assert.equal(consumeEmployeePreviewRequest(), false, '키가 없으면 아무 일도 없다');

put({ open: 'picker', createdAt: Date.now() });
assert.equal(consumeEmployeePreviewRequest(), true);
assert.equal(picker.classes.has('hidden'), false);
assert.equal(store.has(KEY), false, '읽는 즉시 삭제');
assert.equal(consumeEmployeePreviewRequest(), false, '두 번째는 없다');
closeEmployeePreviewPicker();

put({ email: 'kim@example.com', name: '김철수', createdAt: Date.now() - 60 * 1000 });
assert.equal(consumeEmployeePreviewRequest(), true);
assert.equal(employeePreview.email, 'kim@example.com');
assert.equal(employeePreview.name, '김철수');
assert.equal(store.has(KEY), false);
stopEmployeePreview();

// 만료(5분 초과)·미래 시각·시각 없음·깨진 값 → 무시 후 삭제
put({ open: 'picker', createdAt: Date.now() - 5 * 60 * 1000 - 1000 });
assert.equal(consumeEmployeePreviewRequest(), false);
assert.equal(picker.classes.has('hidden'), true);
assert.equal(store.has(KEY), false);
put({ open: 'picker', createdAt: Date.now() + 60 * 60 * 1000 });
assert.equal(consumeEmployeePreviewRequest(), false);
assert.equal(store.has(KEY), false);
put({ open: 'picker' });
assert.equal(consumeEmployeePreviewRequest(), false);
assert.equal(store.has(KEY), false);
put('{not json');
assert.equal(consumeEmployeePreviewRequest(), false);
assert.equal(store.has(KEY), false);
put({ createdAt: Date.now() });
assert.equal(consumeEmployeePreviewRequest(), false, '대상 정보가 없으면 무시');
assert.equal(store.has(KEY), false);

// 권한 없음 → 무시 후 삭제, 미리보기 시작 안 함
setPermissions({ role: 'employee', can_manage_onboarding: false });
put({ email: 'kim@example.com', name: '김철수', createdAt: Date.now() });
assert.equal(consumeEmployeePreviewRequest(), false);
assert.equal(employeePreview, null);
assert.equal(store.has(KEY), false);
put({ open: 'picker', createdAt: Date.now() });
assert.equal(consumeEmployeePreviewRequest(), false);
assert.equal(picker.classes.has('hidden'), true);
assert.equal(store.has(KEY), false);
dropEmployeePreviewRequest();
"""


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_picker_and_handoff_execute_in_node(tmp_path):
    preview = _block("ADMIN-EMPLOYEE-PREVIEW:BEGIN", "ADMIN-EMPLOYEE-PREVIEW:END")
    handoff = _block("ADMIN-EMPLOYEE-PREVIEW-HANDOFF:BEGIN", "ADMIN-EMPLOYEE-PREVIEW-HANDOFF:END")
    sources = "\n".join(
        [
            preview,
            handoff,
            _function_source(INDEX, "canManageAutomation"),
            _function_source(INDEX, "canManageOnboarding"),
            _function_source(INDEX, "canManageSettings"),
            _function_source(INDEX, "isEmployeeAccount"),
            _function_source(INDEX, "isEmployeeExperience"),
            _function_source(INDEX, "allowedViews"),
            _function_source(INDEX, "defaultView"),
        ]
    )
    out = _run_node(PICKER_HARNESS, {"SOURCES": sources, "CHECKS": PICKER_CHECKS}, tmp_path)
    assert "NODE_OK" in out


V41_HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const store = new Map();
const toasts = [];
const entries = [{ hidden: true }, { hidden: true }];
const location = { href: '' };
let session = { permissions: { can_manage_onboarding: true } };
let failSession = false;
const ctx = {
  JSON, Date, Promise, Map, console, process,
  state: {},
  window: { location, obysContractV41: { rerender() { ctx.rerendered = (ctx.rerendered || 0) + 1; } } },
  document: { querySelectorAll: sel => (sel === '[data-employee-preview-entry]' ? entries : []) },
  sessionStorage: { setItem: (k, v) => store.set(k, v), getItem: k => store.get(k) ?? null },
  showToast: m => toasts.push(m),
  financeApiV41: async path => { assert.equal(path, '/session'); if (failSession) throw new Error('403'); return session; },
  assert, store, toasts, entries, location,
  setSession: v => { session = v; }, setFail: v => { failSession = v; },
};
vm.createContext(ctx);
vm.runInContext(process.env.SOURCES + '\n' + process.env.CHECKS, ctx);
"""

V41_CHECKS = r"""
(async () => {
  // 권한 확인 전에는 숨김, 관리자면 노출
  await loadEmployeePreviewPermissionV41();
  assert.equal(state.canManageOnboarding, true);
  assert.equal(entries.every(e => e.hidden === false), true);
  assert.equal(window.obysEmployeePreviewV41.canManage(), true);
  setSession({ permissions: { can_manage_onboarding: false } });
  await loadEmployeePreviewPermissionV41();
  assert.equal(entries.every(e => e.hidden === true), true, '권한 없음 → 숨김');
  assert.equal(window.obysEmployeePreviewV41.canManage(), false);
  setFail(true);
  await loadEmployeePreviewPermissionV41();
  assert.equal(state.canManageOnboarding, false, '조회 실패 → 숨김');
  assert.equal(entries.every(e => e.hidden === true), true);

  // 메뉴 → 선택창 요청, 행 버튼 → 직원 지정 요청. URL 에는 개인정보가 없다.
  assert.equal(openEmployeePreviewV41({ open: 'picker' }), true);
  assert.equal(location.href, '/static/apps/obys/index.html?legacy=1&view=legacy');
  let saved = JSON.parse(store.get('obys.employeePreviewRequest'));
  assert.equal(saved.open, 'picker');
  assert.equal(Math.abs(Date.now() - saved.createdAt) < 5000, true, '생성시각 포함');

  openEmployeePreviewV41({ email: 'emp@example.com', name: '홍길동' });
  assert.equal(location.href.includes('emp'), false);
  assert.equal(location.href.includes('%40'), false);
  assert.equal(location.href.includes('@'), false);
  assert.equal(location.href.includes('홍길동'), false);
  saved = JSON.parse(store.get('obys.employeePreviewRequest'));
  assert.equal(saved.email, 'emp@example.com');
  console.log('V41_OK');
})().catch(error => { console.error(error); process.exit(1); });
"""

EDITOR_HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const esc = s => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
let canManage = true;
const ctx = { String, esc, window: { obysEmployeePreviewV41: { canManage: () => canManage } }, assert,
  setManage: v => { canManage = v; }, clearPreview: () => { delete ctx.window.obysEmployeePreviewV41; } };
vm.createContext(ctx);
vm.runInContext(process.env.SOURCE + `
const ok = previewButton('Emp@Example.com', '홍길동');
assert.match(ok, /data-cv41-preview-email="emp@example.com"/);
assert.match(ok, /data-cv41-preview-name="홍길동"/);
assert.match(ok, /직원 화면으로 보기/);
assert.equal(previewButton('ab***@example.com', '홍길동'), '', '마스킹 이메일만 있으면 버튼 없음');
assert.equal(previewButton('', '홍길동'), '');
assert.equal(previewButton(undefined, undefined), '');
setManage(false);
assert.equal(previewButton('emp@example.com', '홍길동'), '', '관리자가 아니면 버튼 없음');
setManage(true);
clearPreview();
assert.equal(previewButton('emp@example.com', '홍길동'), '', 'V4.1 진입 객체가 없으면 버튼 없음');
`, ctx);
console.log('EDITOR_OK');
"""


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_v41_handoff_and_permission_execute_in_node(tmp_path):
    sources = "\n".join(
        [
            'const EMPLOYEE_PREVIEW_REQUEST_KEY_V41="obys.employeePreviewRequest";',
            _line_starting(V41, "function openEmployeePreviewV41"),
            _line_starting(V41, "window.obysEmployeePreviewV41="),
            _line_starting(V41, "async function loadEmployeePreviewPermissionV41"),
        ]
    )
    out = _run_node(V41_HARNESS, {"SOURCES": sources, "CHECKS": V41_CHECKS}, tmp_path)
    assert "V41_OK" in out


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_editor_row_button_skips_masked_email_in_node(tmp_path):
    source = _function_source(EDITOR, "previewButton", indent="  ")
    out = _run_node(EDITOR_HARNESS, {"SOURCE": source}, tmp_path)
    assert "EDITOR_OK" in out


RETURN_HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const store = new Map();
const replaced = [];
const calls = { setView: [] };
let permissions = { role: 'admin', can_manage_onboarding: true };
const box = () => ({ classList: { add() {}, remove() {}, contains: () => true, toggle() {} }, value: '', innerHTML: '', focus() {} });
const elements = { employeePreviewPicker: box(), employeePreviewList: box(), employeePreviewSearch: box(),
  employeePreviewBanner: box(), employeePreviewBannerText: { textContent: '' } };
const ctx = {
  document: { body: { classList: { add() {}, remove() {} } }, addEventListener() {},
    getElementById: id => elements[id] || null, querySelectorAll: () => [] },
  window: { location: { hash: '', replace: url => replaced.push(url) } },
  sessionStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
  Promise, Error, Array, String, Boolean, Object, RegExp, JSON, Map, Set, Number, Date, console,
  showToast() {}, renderAll() {}, setView: v => calls.setView.push(v),
  hasServerAuth: () => true, authPermissions: () => permissions, authSession: { user: { email: 'a@example.com' } },
  loadEmployeeSelfSignupMap: () => ({}), loadEmployeeJoinRequests: () => Promise.resolve(), loadEmployeeContracts: () => Promise.resolve(),
  escapeHtml: s => String(s),
  currentView: 'employees',
  appViewNames: ['dashboard', 'employees', 'contracts', 'onboarding'],
  employeeViewNames: ['onboarding', 'contracts'],
  employeeJoinRequests: [], approvedEmployees: [], state: { contracts: [] },
  V41_APP_PATH: '/static/apps/obys/admin.html',
  store, replaced, calls, assert, setPermissions: p => { permissions = p; },
};
vm.createContext(ctx);
vm.runInContext(process.env.SOURCES + '\n' + process.env.CHECKS, ctx);
console.log('RETURN_OK');
"""

RETURN_CHECKS = r"""
const KEY = 'obys.employeePreviewRequest';
const RKEY = 'obys.employeePreviewReturn';
const put = obj => store.set(KEY, JSON.stringify({ createdAt: Date.now(), ...obj }));
const reset = () => { replaced.length = 0; calls.setView.length = 0; };

// 해시 검증
assert.equal(v41RouteHash('#employees'), '#employees');
assert.equal(v41RouteHash('#payroll'), '#payroll');
assert.equal(v41RouteHash('#employees?x=1'), '#home');
assert.equal(v41RouteHash('#unknown'), '#home');
assert.equal(v41RouteHash('javascript:alert(1)'), '#home');
assert.equal(v41RouteHash(undefined), '#home');

// V4.1 출발(직원 지정) → 종료 시 V4.1 해시로 replace, setView 안 함, 보관 키 삭제
put({ email: 'emp@example.com', name: '홍길동', returnTo: 'v41', returnHash: '#payroll' });
assert.equal(consumeEmployeePreviewRequest(), true);
assert.equal(JSON.parse(store.get(RKEY)).returnHash, '#payroll');
assert.equal(stopEmployeePreview(), true);
assert.equal(JSON.stringify(replaced), JSON.stringify(['/static/apps/obys/admin.html#payroll']));
assert.equal(calls.setView.filter(v => v === 'dashboard').length, 0);
assert.equal(store.has(RKEY), false, '복귀 후 보관 키 삭제');
assert.equal(employeePreviewReturnTarget, null);

// 새로고침으로 메모리가 사라져도 sessionStorage 로 복귀
reset();
put({ email: 'emp@example.com', name: '홍길동', returnTo: 'v41', returnHash: '#employees' });
assert.equal(consumeEmployeePreviewRequest(), true);
employeePreviewReturnTarget = null;
stopEmployeePreview();
assert.equal(JSON.stringify(replaced), JSON.stringify(['/static/apps/obys/admin.html#employees']));

// 허용되지 않은 해시 → #home
reset();
put({ email: 'emp@example.com', name: '홍길동', returnTo: 'v41', returnHash: '#evil' });
assert.equal(consumeEmployeePreviewRequest(), true);
assert.equal(JSON.parse(store.get(RKEY)).returnHash, '#home');
stopEmployeePreview();
assert.equal(JSON.stringify(replaced), JSON.stringify(['/static/apps/obys/admin.html#home']));

// 보관 키가 변조돼도 검증
reset();
put({ email: 'emp@example.com', name: '홍길동', returnTo: 'v41', returnHash: '#employees' });
consumeEmployeePreviewRequest();
employeePreviewReturnTarget = null;
store.set(RKEY, JSON.stringify({ returnTo: 'v41', returnHash: 'https://evil.example' }));
stopEmployeePreview();
assert.equal(JSON.stringify(replaced), JSON.stringify(['/static/apps/obys/admin.html#home']));

// 선택창 요청 → 직원 선택 → 종료
reset();
put({ open: 'picker', returnTo: 'v41', returnHash: '#employees' });
assert.equal(consumeEmployeePreviewRequest(), true);
startEmployeePreview({ email: 'emp@example.com', name: '홍길동' });
stopEmployeePreview();
assert.equal(JSON.stringify(replaced), JSON.stringify(['/static/apps/obys/admin.html#employees']));

// 선택창만 열고 취소하면 보관값을 버린다
reset();
put({ open: 'picker', returnTo: 'v41', returnHash: '#employees' });
consumeEmployeePreviewRequest();
discardEmployeePreviewReturn();
assert.equal(store.has(RKEY), false);

// legacy 출발(returnTo 없음) → 기존처럼 이전 화면으로 setView
reset();
currentView = 'contracts';
put({ email: 'emp@example.com', name: '홍길동' });
assert.equal(consumeEmployeePreviewRequest(), true);
assert.equal(store.has(RKEY), false);
stopEmployeePreview();
assert.equal(replaced.length, 0);
assert.equal(calls.setView.at(-1), 'contracts');

// 직접 시작(메뉴/목록 버튼)도 기존 동작
reset();
currentView = 'employees';
startEmployeePreview({ email: 'emp@example.com', name: '홍길동' });
stopEmployeePreview();
assert.equal(replaced.length, 0);
assert.equal(calls.setView.at(-1), 'employees');

// 로그아웃·권한 상실 경로(stay)는 이동하지 않고 보관값만 버린다
reset();
put({ email: 'emp@example.com', name: '홍길동', returnTo: 'v41', returnHash: '#employees' });
consumeEmployeePreviewRequest();
stopEmployeePreview({ stay: true });
assert.equal(replaced.length, 0);
assert.equal(store.has(RKEY), false);
"""


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_preview_exit_returns_to_v41_origin_in_node(tmp_path):
    preview = _block("ADMIN-EMPLOYEE-PREVIEW:BEGIN", "ADMIN-EMPLOYEE-PREVIEW:END")
    handoff = _block("ADMIN-EMPLOYEE-PREVIEW-HANDOFF:BEGIN", "ADMIN-EMPLOYEE-PREVIEW-HANDOFF:END")
    sources = "\n".join(
        [
            preview,
            handoff,
            _function_source(INDEX, "v41RouteHash"),
            _function_source(INDEX, "canManageAutomation"),
            _function_source(INDEX, "canManageOnboarding"),
            _function_source(INDEX, "canManageSettings"),
            _function_source(INDEX, "isEmployeeAccount"),
            _function_source(INDEX, "isEmployeeExperience"),
            _function_source(INDEX, "allowedViews"),
            _function_source(INDEX, "defaultView"),
        ]
    )
    out = _run_node(RETURN_HARNESS, {"SOURCES": sources, "CHECKS": RETURN_CHECKS}, tmp_path)
    assert "RETURN_OK" in out


def test_return_wiring_keeps_v41_detection_untouched():
    assert "returnEmployeePreviewToV41()" in _function_source(INDEX, "stopEmployeePreview")
    assert "v41RouteHash(window.location.hash)" in _function_source(INDEX, "openV41App")
    assert "stopEmployeePreview({ stay: true })" in _function_source(INDEX, "logoutFromServer")
    assert "isEmployeeAccount()" in _function_source(INDEX, "shouldOpenV41App")
