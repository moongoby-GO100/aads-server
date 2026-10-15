"""ACCT-OBYS-ADMIN-EMPLOYEE-VIEW-PREVIEW-20261015 — 관리자 '직원 화면으로 보기'(읽기 전용 미리보기).

서버 변경이 없는 클라이언트 전용 모드다. 새 토큰을 발급하지 않고(`/auth/admin/impersonate` 미사용),
sessionStorage/localStorage 에도 쓰지 않는다. 동작 검증은 index.html 에서 함수 원문을 뽑아 node 로 실행한다.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys" / "index.html"
HTML = INDEX.read_text(encoding="utf-8")


def _preview_block() -> str:
    match = re.search(
        r"// ADMIN-EMPLOYEE-PREVIEW:BEGIN\n(.*?)// ADMIN-EMPLOYEE-PREVIEW:END", HTML, re.S
    )
    assert match, "ADMIN-EMPLOYEE-PREVIEW 블록이 없다"
    return match.group(1)


def _handoff_block() -> str:
    match = re.search(
        r"// ADMIN-EMPLOYEE-PREVIEW-HANDOFF:BEGIN\n(.*?)// ADMIN-EMPLOYEE-PREVIEW-HANDOFF:END", HTML, re.S
    )
    assert match, "ADMIN-EMPLOYEE-PREVIEW-HANDOFF 블록이 없다"
    return match.group(1)


def _function_source(name: str) -> str:
    found = re.search(rf"^    (?:async )?function {name}\(", HTML, re.M)
    assert found, name
    start = found.start()
    open_at = HTML.index(") {", start) + 2
    depth = 0
    for pos in range(open_at, len(HTML)):
        char = HTML[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return HTML[start : pos + 1]
    raise AssertionError(name)


class TestStaticWiring:
    def test_banner_and_exit_button_exist(self):
        assert 'id="employeePreviewBanner"' in HTML
        assert 'id="employeePreviewBannerText"' in HTML
        assert 'id="employeePreviewExitBtn"' in HTML
        assert "관리자 화면으로 돌아가기" in HTML
        assert "님 화면을 보는 중입니다 (읽기 전용)" in HTML

    def test_preview_state_is_memory_only_and_issues_no_token(self):
        block = "\n".join(
            line for line in _preview_block().splitlines() if not line.lstrip().startswith("//")
        )
        assert "sessionStorage" not in block
        assert "localStorage" not in block
        assert "impersonate" not in HTML
        assert "saveAuthSession" not in block

    def test_preview_buttons_are_gated_by_admin_permission(self):
        helper = _function_source("employeePreviewButtonHtml")
        assert "canManageOnboarding()" in helper
        # 직원 목록(가입요청·승인 직원)과 계약서 목록 행에 모두 붙어 있다.
        assert HTML.count("employeePreviewButtonHtml(") >= 4
        join_rows = _function_source("renderEmployeeJoinRows")
        assert 'status === "approved"' in join_rows and "employeePreviewButtonHtml" in join_rows
        assert "employeePreviewButtonHtml" in _function_source("renderContracts")

    def test_api_wrappers_block_writes_in_preview(self):
        for name in ("authApi", "financeApi", "workspaceApi", "inventoryApi"):
            source = _function_source(name)
            assert "assertEmployeePreviewReadOnly(options.method)" in source, name

    def test_preview_overrides_permission_and_identity_helpers(self):
        assert "if (employeePreview) return true;" in _function_source("isEmployeeExperience")
        assert "!employeePreview" in _function_source("canManageOnboarding")
        assert "!employeePreview" in _function_source("canManageAutomation")
        assert "!employeePreview" in _function_source("canManageSettings")
        assert "employeePreview.email" in _function_source("currentEmployeeEmail")
        assert "employeePreview.name" in _function_source("currentEmployeeName")
        assert "employeePreview" in _function_source("refreshSignPrompt")
        assert "stopEmployeePreview({ stay: true })" in _function_source("logoutFromServer")
        assert "syncEmployeePreviewSession()" in _function_source("applyAccessPolicy")


NODE_HARNESS = r"""
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
    let m = token.match(/^(\w+)?\[([\w-]+)(?:=(\w+))?\]$/);
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

const listeners = {};
const toasts = [];
const calls = { renderAll: 0, setView: [], nativeFetch: [] };
const controls = [];
const bannerClasses = new Set(['hidden']);
const bodyClasses = new Set();
const bannerText = { textContent: '' };
const banner = { classList: { toggle: (c, on) => (on ? bannerClasses.add(c) : bannerClasses.delete(c)) } };
let permissions = { role: 'admin', can_manage_onboarding: true };

const ctx = {
  document: {
    body: { classList: { add: c => bodyClasses.add(c), remove: c => bodyClasses.delete(c) } },
    addEventListener: (type, fn, capture) => { listeners[type] = { fn, capture }; },
    getElementById: id => (id === 'employeePreviewBanner' ? banner : id === 'employeePreviewBannerText' ? bannerText : null),
    querySelectorAll: () => controls,
  },
  window: { fetch: (url, init) => { calls.nativeFetch.push([url, init && init.method]); return Promise.resolve({ ok: true }); } },
  Promise, Error, Array, String, Boolean, Object, RegExp, JSON, console, exit: code => process.exit(code),
  requestAnimationFrame: fn => fn(),
  showToast: m => toasts.push(m),
  renderAll: () => { calls.renderAll += 1; },
  setView: v => { calls.setView.push(v); },
  hasServerAuth: () => true,
  authPermissions: () => permissions,
  authSession: { user: { email: 'admin@example.com', name: '관리자' } },
  loadEmployeeSelfSignupMap: () => ({}),
  escapeHtml: s => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;'),
  currentView: 'contracts',
  appViewNames: ['dashboard', 'salesLedger', 'employees', 'contracts', 'attendance', 'onboarding', 'payroll'],
  employeeViewNames: ['attendance', 'onboarding', 'contracts', 'payroll'],
  FakeEl, controls, listeners, toasts, calls, bannerClasses, bodyClasses, bannerText,
  setPermissions: p => { permissions = p; },
  assert,
};
vm.createContext(ctx);
vm.runInContext(process.env.SOURCES + '\n' + process.env.CHECKS, ctx);
console.log('NODE_OK');
"""

CHECKS = r"""
function ev(target) {
  return { target, defaultPrevented: false, stopped: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; } };
}
const signBtn = new FakeEl({ attrs: { 'data-sign-contract': 'c1' }, text: '내용 확인·서명' });
const pdfBtn = new FakeEl({ attrs: { 'data-download-contract-pdf': 'c1' }, text: '서명본 PDF' });
const openBtn = new FakeEl({ attrs: { 'data-download-onboarding': 'd1' }, text: '열기' });
const uploadBtn = new FakeEl({ tag: 'button', attrs: { type: 'submit' }, text: '업로드' });
const fileInput = new FakeEl({ tag: 'input', attrs: { type: 'file' } });
const deleteBtn = new FakeEl({ attrs: { 'data-delete-onboarding': 'd1' }, text: '-' });
const tabBtn = new FakeEl({ attrs: { 'data-view': 'contracts' }, text: '계약서 등록', classes: ['tab'] });
const exitBtn = new FakeEl({ id: 'employeePreviewExitBtn', text: '관리자 화면으로 돌아가기' });
controls.push(signBtn, pdfBtn, openBtn, uploadBtn, fileInput, deleteBtn, tabBtn);

// 1. 진입 전 — 관리자 화면, 버튼 노출
assert.equal(isEmployeeExperience(), false);
assert.equal(canManageOnboarding(), true);
const btnHtml = employeePreviewButtonHtml('Emp@Example.com', '홍길동');
assert.match(btnHtml, /data-preview-employee/);
assert.match(btnHtml, /data-preview-email="emp@example.com"/);
assert.match(btnHtml, /직원 화면으로 보기/);
assert.equal(employeePreviewButtonHtml('ab***@x.com', '').length, 0, '마스킹 이메일뿐이면 대상을 특정할 수 없다');
assert.match(employeePreviewButtonHtml('ab***@x.com', '홍길동'), /data-preview-email=""/);

// 2. 비관리자에게는 버튼 미노출 + 진입 거부
setPermissions({ role: 'employee', can_manage_onboarding: false });
assert.equal(employeePreviewButtonHtml('emp@example.com', '홍길동'), '');
assert.equal(startEmployeePreview({ email: 'emp@example.com', name: '홍길동' }), false);
assert.equal(isEmployeeExperience(), true);
setPermissions({ role: 'viewer', can_manage_onboarding: false });
assert.equal(employeePreviewButtonHtml('emp@example.com', '홍길동'), '');
assert.equal(startEmployeePreview({ email: 'emp@example.com', name: '홍길동' }), false);
assert.equal(employeePreview, null);
setPermissions({ role: 'admin', can_manage_onboarding: true });
toasts.length = 0;

// 3. 진입 — 직원 메뉴 4개 제한, 배너, 대상 기준 필터
assert.equal(startEmployeePreview({ email: 'Emp@Example.com', name: '홍길동' }), true);
assert.equal(isEmployeeExperience(), true);
assert.equal(JSON.stringify(allowedViews()), JSON.stringify(['attendance', 'onboarding', 'contracts', 'payroll']));
assert.equal(defaultView(), 'onboarding');
assert.equal(canManageOnboarding(), false);
assert.equal(canManageAutomation(), false);
assert.equal(canManageSettings(), false);
assert.equal(currentEmployeeEmail(), 'emp@example.com');
assert.equal(currentEmployeeName(), '홍길동');
assert.equal(bodyClasses.has('employee-preview'), true);
assert.equal(bannerClasses.has('hidden'), false);
assert.equal(bannerText.textContent, '홍길동 님 화면을 보는 중입니다 (읽기 전용)');
assert.equal(calls.renderAll, 1);
assert.equal(calls.setView.at(-1), 'contracts', '직원 메뉴에 있는 화면은 그대로 유지');
assert.equal(employeePreviewButtonHtml('emp@example.com', '홍길동'), '', '미리보기 중 중첩 진입 버튼 없음');
assert.equal(startEmployeePreview({ email: 'other@example.com', name: '다른사람' }), false);
assert.equal(currentEmployeeEmail(), 'emp@example.com');

assert.equal(isCurrentEmployeeRecord({ employee_email: 'EMP@example.com' }), true);
assert.equal(isCurrentEmployeeRecord({ employee_email: 'other@example.com', employee_name: '홍길동' }), false);
assert.equal(isCurrentEmployeeRecord({ employee_name: '홍길동' }), true);
assert.equal(isCurrentEmployeeRecord({ employeeName: '김철수' }), false);
assert.equal(isCurrentEmployeeRecord({}), false);

// 4. 쓰기 차단 — 화면(비활성·클릭·제출)
assert.equal(signBtn.disabled, true);
assert.equal(uploadBtn.disabled, true);
assert.equal(fileInput.disabled, true);
assert.equal(deleteBtn.disabled, true);
assert.equal(pdfBtn.disabled, false, 'PDF 열람은 읽기');
assert.equal(openBtn.disabled, false, '서류 열기는 읽기');
assert.equal(tabBtn.disabled, false, '메뉴 탭은 막지 않는다');
assert.match(signBtn.title, /읽기 전용/);

const click = ev(signBtn);
listeners.click.fn(click);
assert.equal(click.defaultPrevented && click.stopped, true);
assert.match(toasts.at(-1), /읽기 전용/);
const pdfClick = ev(pdfBtn);
listeners.click.fn(pdfClick);
assert.equal(pdfClick.defaultPrevented, false);
const fileClick = ev(fileInput);
listeners.click.fn(fileClick);
assert.equal(fileClick.defaultPrevented, true);
const submit = ev(new FakeEl({ tag: 'form' }));
listeners.submit.fn(submit);
assert.equal(submit.defaultPrevented, true);

// 5. 쓰기 차단 — API 래퍼와 전역 fetch
for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'post']) {
  assert.throws(() => assertEmployeePreviewReadOnly(method), /읽기 전용/);
}
assert.doesNotThrow(() => assertEmployeePreviewReadOnly('GET'));
assert.doesNotThrow(() => assertEmployeePreviewReadOnly());
assert.doesNotThrow(() => assertEmployeePreviewReadOnly('HEAD'));
(async () => {
  await assert.rejects(window.fetch('/api/v1/yeoljeong-finance/contracts/c1/sign', { method: 'POST' }), /읽기 전용/);
  await assert.rejects(window.fetch('/api/v1/auth/logout', { method: 'POST' }), /읽기 전용/);
  await window.fetch('/api/v1/yeoljeong-finance/contracts', { method: 'GET' });
  await window.fetch('/api/v1/yeoljeong-finance/contracts');
  assert.equal(JSON.stringify(calls.nativeFetch.map(c => c[1] || 'GET')), JSON.stringify(['GET', 'GET']));
  assert.equal(calls.nativeFetch.length, 2, 'POST 는 네이티브 fetch 까지 가지 않는다');

  // 6. 종료 — 버튼으로 돌아가기
  const exitClick = ev(exitBtn);
  listeners.click.fn(exitClick);
  assert.equal(exitClick.defaultPrevented, true);
  assert.equal(employeePreview, null);
  assert.equal(isEmployeeExperience(), false);
  assert.equal(canManageOnboarding(), true);
  assert.equal(bodyClasses.has('employee-preview'), false);
  assert.equal(bannerClasses.has('hidden'), true);
  assert.equal(signBtn.disabled, false);
  assert.equal(uploadBtn.disabled, false);
  assert.equal(signBtn.title, '');
  assert.equal(calls.setView.at(-1), 'contracts', '진입 직전 화면으로 복귀');
  const afterSubmit = ev(new FakeEl({ tag: 'form' }));
  listeners.submit.fn(afterSubmit);
  assert.equal(afterSubmit.defaultPrevented, false);
  await window.fetch('/api/v1/yeoljeong-finance/contracts', { method: 'POST' });
  assert.equal(calls.nativeFetch.length, 3, '종료 후에는 쓰기 허용');

  // 7. 미리보기 중 관리자 권한이 사라지면(로그아웃·세션 만료) 자동 종료
  startEmployeePreview({ email: 'emp@example.com', name: '홍길동' });
  assert.equal(isEmployeeExperience(), true);
  setPermissions({ role: 'viewer', can_manage_onboarding: false });
  syncEmployeePreviewSession();
  assert.equal(employeePreview, null);
  console.log('ASYNC_OK');
})().catch(error => { console.error(error); exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node 미설치 런타임")
def test_preview_lifecycle_and_write_block_execute_in_node(tmp_path):
    sources = "\n".join(
        [
            _preview_block(),
            _handoff_block(),
            _function_source("v41RouteHash"),
            _function_source("canManageAutomation"),
            _function_source("canManageOnboarding"),
            _function_source("canManageSettings"),
            _function_source("isEmployeeAccount"),
            _function_source("isEmployeeExperience"),
            _function_source("allowedViews"),
            _function_source("defaultView"),
            _function_source("currentEmployeeEmail"),
            _function_source("currentEmployeeName"),
            _function_source("isCurrentEmployeeRecord"),
        ]
    )
    # 블록의 let 선언은 같은 vm 스크립트 안에서만 공유되므로 함께 실행한다.
    script = tmp_path / "harness.js"
    script.write_text(NODE_HARNESS, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script)],
        text=True,
        capture_output=True,
        timeout=30,
        env={"SOURCES": sources, "CHECKS": CHECKS, "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NODE_OK" in result.stdout
    assert "ASYNC_OK" in result.stdout
