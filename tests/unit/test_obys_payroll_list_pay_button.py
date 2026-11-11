"""오비서 급여 현황 목록 '지급 처리' 버튼 동작 및 대기/안내/취소 검증.

CEO 보고(2026-11-11):
급여 현황 목록에서 확정된 명세 행의 '지급 처리' 버튼을 누르면 화면에 아무 변화가 없는 문제 수정.

검증 항목:
(a) pay act + 활성 버튼 → click 호출
(b) 버튼이 늦게 활성화돼도 대기 후 click
(c) 시간 초과 시 안내 문구 및 편집 영역 스크롤
(d) 대기 중 다른 행 선택 또는 월/지점 변경 시 자동 click 취소
(e) confirm / draft 등 기존 act 동작(scroll + focus, click 없음) 보존
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys" / "admin.html"
SRC = HTML.read_text(encoding="utf-8")

PREFIXES = (
    "function payrollActionV41(",
    "function payrollFocusActionV41(",
)


def _lines():
    out = []
    for prefix in PREFIXES:
        out.append(next(line.strip() for line in SRC.splitlines() if line.strip().startswith(prefix)))
    return "\n".join(out)


HARNESS = r"""const assert = require('node:assert/strict');

const state = {
  route: 'payroll',
  peopleMonth: '2026-11',
  payrollMonth: '2026-11',
  peopleBranch: '',
  payrollDetailKey: '',
  payrollPanelAction: '',
  payrollEmployeeId: '',
  payrollFocusSeq: 0,
  payrollFocusTimer: null,
};

let clicks = 0;
let focuses = 0;
let scrolls = [];
let editorScrolls = [];

const payButton = {
  disabled: false,
  click() { clicks++; },
  focus() { focuses++; },
  scrollIntoView(opt) { scrolls.push(opt); }
};

const confirmButton = {
  disabled: false,
  click() { clicks++; },
  focus() { focuses++; },
  scrollIntoView(opt) { scrolls.push(opt); }
};

const editorState = { textContent: '' };
const editorMessage = { textContent: '' };
const employeeSelect = { value: 'emp-1' };

const editor = {
  scrollIntoView(opt) { editorScrolls.push(opt); },
  querySelector(sel) {
    if (sel === '[data-payroll-action="pay"]') return payButton;
    if (sel === '[data-payroll-action="confirm"]') return confirmButton;
    if (sel === '[data-payroll-state]') return editorState;
    if (sel === '[data-payroll-message]') return editorMessage;
    if (sel === '[data-payroll-employee]') return employeeSelect;
    return null;
  }
};

const document = {
  getElementById(id) {
    if (id === 'payrollEditor') return editor;
    return null;
  },
  querySelector(sel) {
    if (sel.startsWith('#payrollEditor ')) {
      return editor.querySelector(sel.replace('#payrollEditor ', ''));
    }
    return null;
  }
};

function resetHarness() {
  clicks = 0;
  focuses = 0;
  scrolls = [];
  editorScrolls = [];
  payButton.disabled = false;
  confirmButton.disabled = false;
  editorState.textContent = '';
  editorMessage.textContent = '';
  employeeSelect.value = 'emp-1';
  state.payrollDetailKey = 'p2';
  state.payrollMonth = '2026-11';
  state.peopleBranch = '';
  state.payrollPanelAction = '';
  state.payrollEmployeeId = 'emp-1';
  state.payrollFocusSeq = 0;
  if (state.payrollFocusTimer) {
    clearInterval(state.payrollFocusTimer);
    state.payrollFocusTimer = null;
  }
}
"""


def _run(body: str, tmp_path: Path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node 미설치")
    script = tmp_path / "test_pay_button.cjs"
    script.write_text(
        HARNESS + "\n" + _lines() + "\n(async () => {\n" + body + "\n})().catch(e => { console.error(e); process.exit(1); });\n",
        encoding="utf-8",
    )
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr + "\n" + done.stdout


def test_payroll_action_v41_returns_pay_for_confirmed():
    assert 'status==="confirmed"?{act:"pay",label:"지급 처리"}' in SRC


def test_pay_action_immediate_click_when_already_ready(tmp_path: Path):
    _run(r"""
resetHarness();
state.payrollPanelAction = 'pay';
state.payrollDetailKey = 'p2';
payButton.disabled = false;

const res = await payrollFocusActionV41();
assert.equal(res, true);
assert.equal(clicks, 1, '활성화된 버튼은 즉시 click() 호출');
assert.equal(focuses, 1, 'focus() 호출');
assert.equal(scrolls.length, 1, 'scrollIntoView 호출');
assert.deepEqual(scrolls[0], { block: 'center' });
assert.equal(state.payrollPanelAction, '', '액션 초기화 확인');
""", tmp_path)


def test_pay_action_waits_for_delayed_activation_and_clicks(tmp_path: Path):
    _run(r"""
resetHarness();
state.payrollPanelAction = 'pay';
state.payrollDetailKey = 'p2';
payButton.disabled = true;

setTimeout(() => {
  payButton.disabled = false;
}, 120);

const res = await payrollFocusActionV41({ timeoutMs: 1000, intervalMs: 40 });
assert.equal(res, true);
assert.equal(clicks, 1, '지연 활성화 후 click() 호출 성공');
assert.equal(focuses, 1);
assert.equal(scrolls.length, 1);
""", tmp_path)


def test_pay_action_timeout_shows_notice_and_scrolls_editor(tmp_path: Path):
    _run(r"""
resetHarness();
state.payrollPanelAction = 'pay';
state.payrollDetailKey = 'p2';
payButton.disabled = true;

const res = await payrollFocusActionV41({ timeoutMs: 150, intervalMs: 30 });
assert.equal(res, false);
assert.equal(clicks, 0, '시간 초과 시 click() 미호출');
assert.equal(editorScrolls.length, 1, '편집 영역으로 scrollIntoView 호출');
assert.deepEqual(editorScrolls[0], { block: 'start' });

const expected = '지급 처리할 명세를 불러오지 못했습니다. 명세 상태(확정 여부)를 확인하세요.';
assert.equal(editorState.textContent, expected, '상태 영역에 안내 문구 표시');
assert.equal(editorMessage.textContent, expected, '메시지 영역에도 안내 문구 표시');
""", tmp_path)


def test_pay_action_cancelled_when_other_row_selected(tmp_path: Path):
    _run(r"""
resetHarness();
state.payrollPanelAction = 'pay';
state.payrollDetailKey = 'p2';
payButton.disabled = true;

const p = payrollFocusActionV41({ timeoutMs: 1000, intervalMs: 40 });

// 대기 중 60ms 시점에 사용자가 다른 직원(p3) 클릭
setTimeout(() => {
  state.payrollDetailKey = 'p3';
}, 60);

// 120ms 시점에 버튼 활성화
setTimeout(() => {
  payButton.disabled = false;
}, 120);

const res = await p;
assert.equal(res, false, '낡은 액션은 취소되어 false 반환');
assert.equal(clicks, 0, '다른 행 클릭 시 자동 click() 취소');
assert.equal(editorState.textContent, '', '취소된 경우 타임아웃 오류 문구 미표시');
""", tmp_path)


def test_pay_action_cancelled_when_month_or_branch_changes(tmp_path: Path):
    _run(r"""
resetHarness();
state.payrollPanelAction = 'pay';
state.payrollDetailKey = 'p2';
payButton.disabled = true;

const p = payrollFocusActionV41({ timeoutMs: 1000, intervalMs: 40 });

// 대기 중 60ms 시점에 급여월 변경
setTimeout(() => {
  state.payrollMonth = '2026-10';
  state.payrollDetailKey = '';
}, 60);

setTimeout(() => {
  payButton.disabled = false;
}, 120);

const res = await p;
assert.equal(res, false);
assert.equal(clicks, 0, '월 변경 시 자동 click() 취소');
""", tmp_path)


def test_confirm_action_does_not_click_only_focuses(tmp_path: Path):
    _run(r"""
resetHarness();
state.payrollPanelAction = 'confirm';
state.payrollDetailKey = 'p1';

const res = await payrollFocusActionV41();
assert.equal(res, true);
assert.equal(clicks, 0, 'confirm 액션은 click() 호출하지 않음 (기존 동작 보존)');
assert.equal(focuses, 1, 'confirm 액션 focus() 호출');
assert.equal(scrolls.length, 1);
assert.equal(state.payrollPanelAction, '', '액션 초기화 확인');
""", tmp_path)
