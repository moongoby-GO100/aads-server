"""ACCT-OBYS-EMPLOYEE-PORTAL-MOCKUP-V1-20261015 — 직원 포털(mockup-employee-contract-v1.html 화면 1~5).

modules/employee-portal.js 는 직원(또는 관리자 '직원 화면 보기')일 때 index.html 의 #employeePortal 에 그린다.
서버 API 는 새로 만들지 않는다 — 기존 POST /contracts/signing 과 GET /contracts/{id}/signing-view 만 쓴다.
동작 검증은 모듈을 node vm 에서 가짜 bridge 로 돌리고, 역할별 진입은 index.html 의 함수 원문을 뽑아 실행한다.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OBYS = ROOT / "app" / "static" / "apps" / "obys"
INDEX = OBYS / "index.html"
HTML = INDEX.read_text(encoding="utf-8")
PORTAL_JS_PATH = OBYS / "modules" / "employee-portal.js"
PORTAL_CSS_PATH = OBYS / "modules" / "employee-portal.css"
PORTAL_JS = PORTAL_JS_PATH.read_text(encoding="utf-8")
PORTAL_CSS = PORTAL_CSS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

MOCKUP_PHRASES = ("근로계약서 서명이 필요합니다", "내 계약서", "동의하고 서명하기", "서명 제출", "서명이 완료됐습니다")


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


def _run_node(script: str, env: dict, tmp_path: Path) -> str:
    path = tmp_path / "harness.js"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(path)],
        text=True,
        capture_output=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", **env},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


class TestStaticWiring:
    def test_module_files_are_loaded_with_version_stamp(self):
        assert '<link rel="stylesheet" href="/static/apps/obys/modules/employee-portal.css?v=' in HTML
        assert '<script src="/static/apps/obys/modules/employee-portal.js?v=' in HTML
        # 메인 인라인 스크립트(init)보다 먼저 정의돼야 서명 링크가 열릴 때 포털이 이미 있다.
        assert HTML.index("modules/employee-portal.js") < HTML.index("const STORE_KEY = ")

    def test_portal_root_sits_beside_admin_shell(self):
        assert '<div id="employeePortal" class="ep-root hidden"' in HTML
        assert HTML.index('id="employeePortal"') < HTML.index('<div id="appShell"')

    def test_mockup_phrases_exist_in_portal_module(self):
        for phrase in MOCKUP_PHRASES:
            assert phrase in PORTAL_JS, phrase

    def test_css_selectors_are_prefixed(self):
        body = re.sub(r"/\*.*?\*/", "", PORTAL_CSS, flags=re.S)
        selectors = []
        for block in re.finditer(r"([^{}]+)\{[^{}]*\}", re.sub(r"@media[^{]*\{", "", body)):
            selectors += [part.strip() for part in block.group(1).split(",") if part.strip()]
        assert selectors
        for selector in selectors:
            assert "ep-" in selector, selector
        widths = {int(w) for w in re.findall(r"@media[^{]*?\(max-width:\s*(\d+)px\)", PORTAL_CSS)}
        assert widths == {760}

    def test_portal_uses_only_existing_api_paths(self):
        assert "fetch(" not in PORTAL_JS
        paths = set(re.findall(r'financeApi\(\s*[`"]([^`"]+)[`"]', PORTAL_JS))
        assert paths == {"/contracts/signing", "/contracts/${encodeURIComponent(contractId)}/signing-view"}
        assert "/contracts/signing" in _function_source("submitContractSignature")
        assert "/signing-view" in _function_source("startPendingSigning")

    def test_index_bridge_and_hooks(self):
        assert "return hasServerAuth() && isEmployeeExperience();" in _function_source("employeePortalActive")
        hand_off = _function_source("openContractSignModal")
        assert hand_off.index("employeePortalActive()") < hand_off.index("canManageOnboarding()")
        assert "window.obysEmployeePortal.openContract(contract, bundle" in hand_off
        assert "syncEmployeePortal();" in _function_source("renderAll")
        assert "queueEmployeePortalSync();" in _function_source("setView")
        for name in ("renderContracts", "renderPayroll", "renderOnboarding", "renderSignPendingStrip"):
            assert "queueEmployeePortalSync();" in _function_source(name), name
        assert "!employeePortalActive()" in _function_source("refreshSignPrompt")
        bridge = _function_source("employeePortalBridge")
        for needle in ("contractPreviewHtml", "downloadSignedContractPdf", "visibleContracts()", "Boolean(employeePreview)", "setView"):
            assert needle in bridge, needle

    def test_admin_v41_and_mockups_untouched(self):
        v41 = (OBYS / "mockup-v4-1.html").read_text(encoding="utf-8")
        assert "employee-portal" not in v41
        should_open = _function_source("shouldOpenV41App")
        assert "if (hasServerAuth() && isEmployeeAccount()) return false;" in should_open
        assert "employeePortal" not in should_open

    def test_release_assets_include_new_modules(self, tmp_path):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import obys_release_assets as tool
        finally:
            sys.path.pop(0)
        urls = tool.purge_urls(OBYS, "abc1234", "https://fb.newtalk.kr")
        base = "https://fb.newtalk.kr/static/apps/obys/modules"
        for name in ("employee-portal.js", "employee-portal.css"):
            assert f"{base}/{name}" in urls and f"{base}/{name}?v=abc1234" in urls
        d = tmp_path / "obys"
        d.mkdir()
        (d / "index.html").write_bytes(INDEX.read_bytes())
        (d / "sw.js").write_bytes((OBYS / "sw.js").read_bytes())
        tool.stamp_tree(d, "abc1234")
        stamped = (d / "index.html").read_text(encoding="utf-8")
        assert "modules/employee-portal.js?v=abc1234" in stamped and "modules/employee-portal.css?v=abc1234" in stamped


ROLE_HARNESS = r"""
const vm = require('node:vm');
const sources = process.env.SOURCES;
const out = {};
for (const c of JSON.parse(process.env.CASES)) {
  const ctx = {
    window: { location: { search: '', hash: '' } }, URLSearchParams, String, Boolean, Array,
    pendingPhoneInviteToken: '',
    employeePreview: c.preview ? { email: 'e@x.kr', name: '홍길동' } : null,
    authSession: { user: { email: 'a@x.kr' }, permissions: c.permissions },
    fallbackPermissions: { role: 'guest' },
    hasServerAuth: () => true,
    readPostLoginRedirectFromUrl: () => '',
    loadEmployeeSelfSignupMap: () => ({}),
    employeePendingPermissions: () => ({ role: 'employee_pending' }),
  };
  vm.createContext(ctx);
  vm.runInContext(sources + '\nthis.r = { v41: shouldOpenV41App(), portal: employeePortalActive(), readOnly: Boolean(employeePreview) };', ctx);
  out[c.name] = ctx.r;
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_role_entry_employee_portal_admin_v41_preview_read_only(tmp_path):
    names = ["authPermissions", "isEmployeeAccount", "isEmployeeExperience", "shouldOpenV41App", "employeePortalActive"]
    cases = [
        {"name": "employee", "permissions": {"role": "employee"}},
        {"name": "employee_pending", "permissions": {"role": "employee_pending"}},
        {"name": "admin", "permissions": {"role": "owner", "can_manage_onboarding": True}},
        {"name": "admin_preview", "permissions": {"role": "owner", "can_manage_onboarding": True}, "preview": True},
    ]
    out = json.loads(_run_node(ROLE_HARNESS, {"SOURCES": "\n".join(_function_source(n) for n in names), "CASES": json.dumps(cases)}, tmp_path))
    assert out["employee"] == {"v41": False, "portal": True, "readOnly": False}
    assert out["employee_pending"]["portal"] is True and out["employee_pending"]["v41"] is False
    assert out["admin"] == {"v41": True, "portal": False, "readOnly": False}
    assert out["admin_preview"] == {"v41": True, "portal": True, "readOnly": True}


PORTAL_HARNESS = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const ctx = { console, setTimeout, Promise };
vm.createContext(ctx);
vm.runInContext(process.env.PORTAL_JS, ctx);
const EP = ctx.obysEmployeePortal;
const core = EP.core;
const writeText = new RegExp(process.env.WRITE_TEXT_RE);
const safeText = new RegExp(process.env.SAFE_TEXT_RE);
const indexPayloadKeys = JSON.parse(process.env.INDEX_PAYLOAD_KEYS);

const C = {
  pending: { id: 'c-req', status: 'requested', contract_type: 'regular', print_title: '표준근로계약서 (기간의 정함이 없는 경우)', employee_name: '홍길동',
    employee_email: 'hong@x.kr', branch: '열정국밥 ○○점', start_date: '2026-10-16', wage: 3200000, wage_type: 'monthly', work_days: '화~토',
    work_time: '10:00-20:00', rest_time: '14:30-16:30', holidays: '주휴 월', pay_date: '10', requested_at: '2026-10-09T10:00:00+09:00' },
  signed: { id: 'c-ok', status: 'signed', contract_type: 'confidentiality', print_title: '개인정보 수집·이용 동의서', branch: '열정국밥 ○○점', signed_at: '2026-07-01T09:00:00+09:00' },
  old: { id: 'c-old', status: 'signed', contract_type: 'part_time', print_title: '단시간근로자 표준근로계약서', branch: '열정국밥 ○○점', wage: 10030, wage_type: 'hourly',
    start_date: '2026-07-01', end_date: '2026-10-15', signed_at: '2026-06-30T09:00:00+09:00', superseded_by: 'c-req' },
  cancelled: { id: 'c-cancel', status: 'cancelled', print_title: '취소된 계약', updated_at: '2026-05-01' },
  draft: { id: 'c-draft', status: 'draft', print_title: '작성중 계약' },
};

function buttons(html) {
  return [...html.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)].map(m => ({
    attrs: m[1], text: m[2].replace(/<span class="ep-m-only">[\s\S]*?<\/span>/g, '').replace(/<[^>]+>/g, '').trim(),
    disabled: /\sdisabled(\s|$|=)/.test(m[1]),
  }));
}
// index.html 미리보기 잠금 규칙(isEmployeePreviewWriteControl)의 글자 기준 — data-preview-allow 가 있으면 잠그지 않는다.
function previewLocked(btn) {
  if (/data-preview-allow/.test(btn.attrs)) return false;
  if (safeText.test(btn.text)) return false;
  return writeText.test(btn.text);
}

function makeBridge(over = {}) {
  const calls = { api: [], setView: [], signed: [], expired: 0, toasts: [] };
  const bridge = {
    root: null, active: true, readOnly: false, employeeEmail: 'hong@x.kr', employeeName: '홍길동', currentView: 'onboarding', today: '2026-10-15',
    contracts: [C.signed, C.old, C.draft, C.pending, C.cancelled], pendingSign: [{ id: 'c-req', expires_at: '2026-10-23T10:00:00+09:00' }],
    payroll: [], onboardingDocs: [{ document_type: 'id_card', status: 'approved' }],
    requiredDocTypes: [{ type: 'id_card', label: '신분증' }, { type: 'bankbook', label: '통장사본' }],
    labels: { contractType: {} },
    contractPreviewHtml: c => `<h3>근로계약서</h3><p>${c.employee_name || ''}</p>`,
    financeApi: async (path, options) => { calls.api.push([path, options]); throw new Error('unexpected api ' + path); },
    setView: v => { calls.setView.push(v); bridge.currentView = v; },
    showToast: m => calls.toasts.push(m),
    handleExpiredServerSession: () => { calls.expired += 1; },
    downloadPdf: async () => {},
    onSigned: payload => calls.signed.push(payload),
    ...over,
  };
  return { bridge, calls };
}
function memoryStorage() {
  const data = {};
  return { data, getItem: k => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, removeItem: k => { delete data[k]; } };
}

(async () => {
  // 1. 목록 정렬·필터 개수 — 서명 대기 → 서명 완료 → 이전 계약, 작성중은 직원에게 보이지 않는다
  const all = [C.signed, C.old, C.draft, C.pending, C.cancelled];
  assert.deepEqual(core.sortContracts(all).map(c => c.id), ['c-req', 'c-ok', 'c-old', 'c-cancel']);
  assert.deepEqual({ ...core.filterCounts(all) }, { all: 4, pending: 1, signed: 1, old: 2 });
  assert.deepEqual(core.filterContracts(all, 'old').map(c => c.id), ['c-old', 'c-cancel']);
  assert.deepEqual(core.filterContracts(all, 'pending').map(c => c.id), ['c-req']);

  // 2. 읽은 정도 계산
  assert.equal(core.readPercent({ scrollTop: 0, scrollHeight: 500, clientHeight: 600 }), 100, '스크롤이 필요 없으면 다 읽음');
  assert.equal(core.readPercent({ scrollTop: 0, scrollHeight: 2000, clientHeight: 500 }), 25);
  assert.equal(core.readPercent({ scrollTop: 1500, scrollHeight: 2000, clientHeight: 500 }), 100);

  // 3. 홈 — 서명 대기가 있으면 맨 위 카드, 목록 화면 문구·개수
  {
    const { bridge, calls } = makeBridge();
    const p = EP.createPortal({ storage: memoryStorage() });
    p.sync(bridge);
    assert.equal(p.state.screen, 'home');
    const home = p.html();
    assert.match(home, /근로계약서 서명이 필요합니다/);
    assert.match(home, /2026-10-09에 요청됨 · 표준근로계약서 \(기간의 정함이 없는 경우\)/);
    assert.match(home, /계약서 읽고 서명하기/);
    assert.match(home, /서명 대기 1건/);
    assert.match(home, /1 \/ 2 제출/);
    assert.match(home, /통장사본 미제출/);
    assert.match(home, /급여일 10일/);
    for (const label of ['홈', '계약서', '급여명세', '근태', '입사서류']) assert.ok(home.includes(`>${label}`), label);
    assert.ok(!/salesLedger|매출|매입|세무/.test(home), '관리자 메뉴 0개');
    p.nav('contracts');
    const list = p.html();
    assert.match(list, /<h3>내 계약서<\/h3>/);
    for (const seg of ['전체 4', '서명 대기 1', '서명 완료 1', '이전 계약 2']) assert.ok(list.includes(seg), seg);
    assert.ok(list.indexOf('c-req') < list.indexOf('c-ok') && list.indexOf('c-ok') < list.indexOf('c-old'));
    assert.ok(!list.includes('c-draft'));
    assert.match(list, /10-09 요청 · 10-23까지/);
    assert.match(list, /열정국밥 ○○점 · 2026-10-16부터 · 월 3,200,000원 · 화~토/);
    assert.match(list, /data-ep-pdf="c-ok"/, '완료본은 PDF 받기');
    assert.ok(!/data-ep-pdf="c-req"/.test(list), '서명 대기는 PDF 없음');
    p.setFilter('old');
    assert.ok(!p.html().includes('data-ep-open="c-req"'));
    // 4. 급여명세·근태·입사서류는 기존 직원 보기로
    p.nav('payroll');
    assert.equal(p.state.screen, 'legacy');
    assert.deepEqual(bridge.currentView, 'payroll');
    p.nav('attendance'); p.nav('onboarding');
    assert.deepEqual(calls.setView, ['payroll', 'attendance', 'onboarding']);
    assert.equal(p.state.legacyTab, 'onboarding');
    bridge.currentView = 'contracts';
    p.sync(bridge);
    assert.equal(p.state.screen, 'list', '기존 화면에서 계약서로 가면 포털 목록');
    // 서명 대기가 없으면 홈 카드가 없다
    const empty = EP.createPortal({ storage: memoryStorage() });
    empty.sync(makeBridge({ contracts: [C.signed] }).bridge);
    assert.ok(!empty.html().includes('서명이 필요합니다'));
    const none = EP.createPortal({ storage: memoryStorage() });
    none.sync(makeBridge({ contracts: [] }).bridge);
    none.nav('contracts');
    assert.match(none.html(), /아직 받은 계약서가 없습니다/);
  }

  // 5. 끝까지 읽기 전 서명 버튼 비활성 → 읽은 뒤 활성 → 체크·서명 없으면 제출 비활성 → 실패 시 서명 유지 → 재시도 성공
  {
    const { bridge, calls } = makeBridge();
    let fail = true;
    bridge.financeApi = async (path, options) => {
      calls.api.push([path, options]);
      if (path === '/contracts/c-req/signing-view') return { contract: { ...C.pending, sign_token: 'tok-1' }, bundle: [] };
      if (path === '/contracts/signing') {
        if (fail) { fail = false; throw new Error('네트워크 오류'); }
        return { contract: { ...C.pending, status: 'signed', signed_at: '2026-10-15T05:20:00Z' }, signed_pdf: { status: 'stored' }, notify: { status: 'sent' } };
      }
      throw new Error('unexpected ' + path);
    };
    const p = EP.createPortal({ storage: memoryStorage() });
    p.sync(bridge);
    p.openContract(C.pending);
    assert.equal(p.state.screen, 'read');
    await new Promise(r => setTimeout(r, 0));
    assert.equal(calls.api[0][0], '/contracts/c-req/signing-view', '목록에서 연 계약은 기존 signing-view 로 토큰을 받는다');
    assert.equal(p.state.token, 'tok-1');
    let read = p.html();
    assert.match(read, /핵심 조건 요약/);
    assert.match(read, /2026-10-16 \(기간 없음\)/);
    assert.match(read, /매월 10일/);
    let go = buttons(read).filter(b => b.text === '동의하고 서명하기');
    assert.equal(go.length, 2, 'PC 오른쪽 칸 + 모바일 하단 고정');
    assert.ok(go.every(b => b.disabled), '끝까지 읽기 전 비활성');
    assert.equal(p.goSign(), false);
    p.setReadProgress(60);
    assert.ok(buttons(p.html()).filter(b => b.text === '동의하고 서명하기').every(b => b.disabled));
    p.setReadProgress(100);
    go = buttons(p.html()).filter(b => b.text === '동의하고 서명하기');
    assert.ok(go.every(b => !b.disabled), '끝까지 읽으면 활성');
    assert.equal(p.goSign(), true);
    assert.equal(p.state.screen, 'sign');

    const submitBtn = () => buttons(p.html()).find(b => b.attrs.includes('data-ep-submit'));
    assert.equal(p.state.sign.name, '홍길동');
    assert.ok(submitBtn().disabled, '서명·체크 없음');
    p.setInk('data:image/png;base64,SIG');
    assert.ok(submitBtn().disabled, '체크 없음');
    p.setConsent('content', true);
    assert.ok(submitBtn().disabled, '체크 1개');
    p.setConsent('esign', true);
    p.setName('');
    assert.ok(submitBtn().disabled, '이름 없음');
    p.setName('홍길동');
    assert.ok(!submitBtn().disabled, '이름·서명·체크 2개 → 활성');
    p.clearInk();
    assert.ok(submitBtn().disabled, '서명 지우면 비활성');
    p.setInk('data:image/png;base64,SIG');

    const first = await p.submit();
    assert.equal(first.reason, 'failed');
    assert.equal(p.state.screen, 'sign');
    assert.equal(p.state.sign.dataUri, 'data:image/png;base64,SIG', '실패해도 서명 유지');
    assert.equal(p.state.sign.consentContent && p.state.sign.consentEsign, true, '체크 유지');
    assert.equal(p.state.sign.name, '홍길동');
    const failed = p.html();
    assert.match(failed, /서명 제출에 실패했습니다: 네트워크 오류/);
    assert.match(failed, /data-ep-consent="content" checked/);
    assert.equal(submitBtn().text, '다시 제출');
    assert.ok(!submitBtn().disabled);

    const second = await p.submit();
    assert.equal(second.ok, true);
    const posts = calls.api.filter(c => c[0] === '/contracts/signing');
    assert.equal(posts.length, 2);
    const body = JSON.parse(posts[1][1].body);
    assert.equal(posts[1][1].method, 'POST');
    assert.deepEqual(Object.keys(body).sort(), indexPayloadKeys.slice().sort(), 'submitContractSignature 와 같은 페이로드');
    assert.deepEqual(body, { token: 'tok-1', signer_name: '홍길동', consent: true, consent_version: 'yeoljeong-contract-sign-v1', signature_data_uri: 'data:image/png;base64,SIG' });
    assert.equal(calls.signed.length, 1);
    assert.equal(p.state.screen, 'done');
    const done = p.html();
    assert.match(done, /서명이 완료됐습니다/);
    assert.match(done, /2026-10-15 14:20 · 표준근로계약서 \(기간의 정함이 없는 경우\) · 관리자에게도 알렸습니다/);
    assert.match(done, /서명본 PDF 받기/);
    assert.match(done, /내 계약서 목록/);
    assert.match(done, /홈으로/);
  }

  // 6. 서명 링크(yf_contract_token)로 연 경우 — 같은 흐름, 링크 토큰 사용, signing-view 를 다시 부르지 않는다
  {
    const { bridge, calls } = makeBridge();
    bridge.financeApi = async (path, options) => { calls.api.push([path, options]); const e = new Error('세션 만료'); e.status = 401; throw e; };
    const storage = memoryStorage();
    const p = EP.createPortal({ storage, now: () => 1000 });
    p.sync(bridge);
    p.openContract(C.pending, [], { token: 'link-tok' });
    assert.equal(calls.api.length, 0);
    p.setReadProgress(100); p.goSign();
    p.setInk('data:image/png;base64,S'); p.setConsent('content', true); p.setConsent('esign', true);
    const r = await p.submit();
    assert.equal(r.reason, 'expired');
    assert.equal(JSON.parse(calls.api[0][1].body).token, 'link-tok');
    assert.equal(calls.expired, 1, '기존 재로그인 흐름');
    assert.equal(JSON.parse(storage.data['obys.employeePortal.resume']).contractId, 'c-req');
    assert.equal(p.state.sign.dataUri, 'data:image/png;base64,S');
    // 새로고침 뒤 다시 로그인 → 보던 계약서로 복귀
    const again = EP.createPortal({ storage, now: () => 2000 });
    again.sync(makeBridge().bridge);
    assert.equal(again.state.screen, 'read');
    assert.equal(again.state.contract.id, 'c-req');
    assert.ok(!('obys.employeePortal.resume' in storage.data), '1회만 쓴다');
  }

  // 7. 관리자 '직원 화면 보기' — 같은 포털, 읽기 전용(제출·수정 요청 비활성, API 호출 없음)
  {
    const { bridge, calls } = makeBridge({ readOnly: true });
    const p = EP.createPortal({ storage: memoryStorage() });
    p.sync(bridge);
    assert.match(p.html(), /근로계약서 서명이 필요합니다/);
    p.openContract(C.pending);
    assert.equal(calls.api.length, 0, '미리보기는 signing-view 도 부르지 않는다');
    const read = p.html();
    const edit = buttons(read).find(b => b.text === '관리자에게 수정 요청');
    assert.ok(edit && edit.disabled, '수정 요청 비활성');
    assert.match(read, /수정 요청 기능은 아직 없습니다/);
    p.setReadProgress(100);
    assert.equal(p.goSign(), true, '서명 창은 볼 수 있다');
    p.setInk('data:image/png;base64,X'); p.setConsent('content', true); p.setConsent('esign', true);
    assert.equal(core.canSubmit(p.state), false);
    const sign = p.html();
    assert.ok(buttons(sign).find(b => b.attrs.includes('data-ep-submit')).disabled);
    assert.match(sign, /읽기 전용 미리보기에서는 서명을 제출할 수 없습니다/);
    const r = await p.submit();
    assert.equal(r.reason, 'read_only');
    assert.equal(calls.api.length, 0, '제출 차단');
    // index.html 미리보기 잠금(글자 기준)과 맞물린다: 제출·수정 요청은 잠기고, 화면 이동 버튼은 잠기지 않는다
    const lockedSign = buttons(sign).filter(previewLocked).map(b => b.text);
    assert.deepEqual(lockedSign, ['서명 제출']);
    const lockedRead = buttons(read).filter(previewLocked).map(b => b.text);
    assert.deepEqual(lockedRead, ['관리자에게 수정 요청']);
    p.nav('contracts');
    const listLocked = buttons(p.html()).filter(previewLocked).map(b => b.text);
    assert.deepEqual(listLocked, ['관리자에게 확인 요청']);
    p.nav('home');
    assert.deepEqual(buttons(p.html()).filter(previewLocked).map(b => b.text), []);
  }

  // 8. 관리자(포털 비활성)·직원 전환
  {
    const { bridge } = makeBridge({ active: false });
    const p = EP.createPortal({ storage: memoryStorage() });
    p.sync(bridge);
    assert.equal(p.isActive(), false);
    const emp = makeBridge();
    p.sync(emp.bridge);
    p.openContract(C.signed);
    const signedRead = p.html();
    assert.match(signedRead, /서명본 PDF 받기/);
    assert.ok(!signedRead.includes('동의하고 서명하기'), '완료본은 내용 보기만');
    emp.bridge.employeeEmail = 'other@x.kr';
    p.sync(emp.bridge);
    assert.equal(p.state.screen, 'home', '다른 직원으로 바뀌면 처음부터');
    assert.equal(p.state.contract, null);
  }
  console.log('NODE_OK');
})().catch(error => { console.error(error); process.exit(1); });
"""


def _preview_regex(name: str) -> str:
    match = re.search(rf"const {name} = /(.*?)/;", HTML)
    assert match, name
    return match.group(1)


def _index_sign_payload_keys() -> list[str]:
    body = _function_source("submitContractSignature")
    block = body[body.index("const body = {") : body.index("};", body.index("const body = {"))]
    return re.findall(r"^\s+(\w+):", block, re.M)


@pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")
def test_portal_flow_executes_in_node(tmp_path):
    keys = _index_sign_payload_keys()
    assert keys == ["token", "signer_name", "consent", "consent_version", "signature_data_uri"]
    out = _run_node(
        PORTAL_HARNESS,
        {
            "PORTAL_JS": PORTAL_JS,
            "WRITE_TEXT_RE": _preview_regex("EMPLOYEE_PREVIEW_WRITE_TEXT_RE"),
            "SAFE_TEXT_RE": _preview_regex("EMPLOYEE_PREVIEW_SAFE_TEXT_RE"),
            "INDEX_PAYLOAD_KEYS": json.dumps(keys),
        },
        tmp_path,
    )
    assert "NODE_OK" in out


def test_portal_hides_v2_bottom_nav_so_sticky_sign_bar_is_reachable():
    hide = re.search(r"((?:body\.ep-on [^{]+,\s*)*body\.ep-on \.v2-bottom-nav[^{]*)\{([^}]*)\}", PORTAL_CSS)
    assert hide, "body.ep-on .v2-bottom-nav 숨김 규칙 없음"
    assert re.search(r"display:\s*none\s*!important", hide.group(2))
    assert re.search(r"body\.ep-on\.v2-enhanced\s*\{\s*padding-bottom:\s*0\s*;?\s*\}", PORTAL_CSS)
    assert re.search(r"\.ep-sticky\s*\{[^}]*position:\s*fixed[^}]*bottom:\s*0", PORTAL_CSS)
    for selector in re.findall(r"([^{}]+)\{[^}]*display:\s*none\s*!important", PORTAL_CSS):
        assert ".ep-sticky" not in selector
