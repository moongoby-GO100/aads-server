"""사람 메뉴 연결과 급여 작업의 회귀 검증. 운영 접속 없음."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / 'app/static/apps/obys/index.html').read_text()
# 운영 이미지에는 node 가 없다. JS 실행 검증은 호스트에서 돈다.
NEEDS_NODE = pytest.mark.skipif(shutil.which('node') is None, reason='node 미설치 런타임')


def test_people_menu_parity_and_invite_submit():
    groups = []
    for nav in ('side-nav', 'sub-tabs'):
        block = re.search(rf'<nav class="{nav}".*?</nav>', HTML, re.S).group()
        groups.append(set(re.findall(r'<(?:a|button)[^>]*data-group="people"[^>]*>(.*?)</(?:a|button)>', block)))
    assert groups[0] == groups[1]
    assert len(groups[0]) == 8
    assert 'id="acceptEmployeeInviteBtn" type="submit"' in HTML
    assert 'acceptEmployeeInvite(els.employeeInviteAcceptForm).catch' in HTML


def test_state_actions_and_destructive_confirmation():
    for action in ('confirm', 'pay', 'versions', 'pdf', 'deliver', 'resend'):
        assert f'"{action}"' in HTML
    assert 'if (!locked ?' not in HTML
    assert '${editable ? `<button class="danger" data-delete-payroll=' in HTML
    assert '지급 완료 기록' in HTML
    for marker in ('if (shiftId) {', 'if (serverShiftId) {'):
        assert 'confirm(' in HTML.split(marker)[1].split('\n', 3)[1]
    assert 'if (!Number(els.contractForm.wage?.value || 0))' in HTML


@NEEDS_NODE
def test_actual_javascript_request_deduplication_and_retry(tmp_path):
    block = HTML.split('// PEOPLE-ACTIONS:BEGIN')[1].split('    async function runPayrollAction')[0]
    source = '''const assert = require('node:assert/strict');
let calls = 0; let reject = false;
function serverAuthToken() { return 'local-test'; }
async function fetch() { calls++; await new Promise(r => setTimeout(r, 15)); if (reject) throw Error('offline'); return new Response('{}'); }
''' + block + '''
(async () => {
 const options = {method:'POST',body:'{}'};
 const responses = await Promise.all([peopleFetch('/api/v1/yeoljeong-finance/payroll/p/confirm',options),peopleFetch('/api/v1/yeoljeong-finance/payroll/p/confirm',options)]);
 assert.equal(calls,1); await responses[0].json(); await responses[1].json();
 // Original transport response must remain unread even after the first consumer finishes.
 let resolveFetch; calls=0;
 fetch=async()=>{calls++;return new Promise(r=>resolveFetch=r)};
 const first=peopleFetch('/payroll/p/confirm',options);
 const second=peopleFetch('/payroll/p/confirm',options);
 resolveFetch(new Response('{}'));
 await (await first).json(); await (await second).json(); assert.equal(calls,1);
 fetch=async()=>{calls++;return new Response('{}')};
 const a=new FormData();a.append('file','one');
 const b=new FormData();b.append('file','two');
 await Promise.all([peopleFetch('/payroll/p/confirm',{method:'POST',body:a}),peopleFetch('/payroll/p/confirm',{method:'POST',body:b})]);
 assert.equal(calls,3);
 const controller=new AbortController();
 await Promise.all([peopleFetch('/payroll/p/confirm',{...options,signal:controller.signal}),peopleFetch('/payroll/p/confirm',options)]);
 assert.equal(calls,5);
 // financeApi·workspaceApi 는 항상 시간 제한 signal 을 넘긴다. 이때도 중복 클릭은 합쳐야 한다.
 const t1=new AbortController(), t2=new AbortController();
 await Promise.all([peopleFetch('/payroll/p/confirm',{...options,signal:t1.signal},{timeoutSignal:true}),peopleFetch('/payroll/p/confirm',{...options,signal:t2.signal},{timeoutSignal:true})]);
 assert.equal(calls,6);
 calls=1;
 fetch=async()=>{calls++;if(reject)throw Error('offline');return new Response('{}')};
 reject = true; await assert.rejects(peopleFetch('/api/v1/yeoljeong-finance/payroll/p/confirm',options));
 reject = false; await peopleFetch('/api/v1/yeoljeong-finance/payroll/p/confirm',options); assert.equal(calls,3);
})();'''
    script = tmp_path / 'request.cjs'
    script.write_text(source)
    result = subprocess.run(['node', str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_session_expiry_and_form_failure_restore_controls():
    assert 'if (response.status === 401 && hasServerAuth()) handleExpiredServerSession();' in HTML
    clock = (ROOT / 'app/static/apps/obys/clock.html').read_text()
    assert 'window.location.replace("/static/apps/obys/index.html#login")' in clock
    for marker in ('els.contractForm.addEventListener("submit", event => {', 'els.payrollForm?.addEventListener("submit", event => {'):
        handler = HTML.split(marker)[1].split('\n      });\n')[0]
        assert 'withPeopleForm(event.currentTarget, async () => {' in handler
        assert '}).catch(error => showToast(`저장 실패: ${error.message}`));' in handler
    payroll_submit = HTML.split('els.payrollForm?.addEventListener("submit", event => {')[1].split('\n      });\n')[0]
    assert 'await loadPayrollStatements();' in payroll_submit
    for api in ('async function financeApi(', 'async function workspaceApi('):
        assert '{ timeoutSignal: !options.signal }' in HTML.split(api)[1].split('return payload;')[0]
    assert '수신 동의 철회' in HTML and 'payroll-delivery-v1' in HTML


@NEEDS_NODE
def test_payroll_actions_run_existing_endpoints_and_recover(tmp_path):
    action = HTML.split('    async function runPayrollAction(button) {')[1].split('    async function createAttendancePayrollDraft')[0]
    source = '''const assert = require('node:assert/strict');
let calls=[], refresh=0, message='', accept=true, fail=false;
const confirm=()=>accept; const canManageOnboarding=()=>true;
const showToast=value=>message=value;
const loadPayrollStatements=async()=>{refresh++};
const financeApi=async(path,options)=>{calls.push([path,options]); await new Promise(r=>setTimeout(r,5));if(fail)throw Error('mock failure');return {status:'failed',delivery:{error_detail:'delivery failure'}}};
async function runPayrollAction(button) {''' + action + '''
(async()=>{
 const b={disabled:false,dataset:{payrollAction:'confirm',statementId:'a/b'}};
 await Promise.all([runPayrollAction(b),runPayrollAction(b)]);
 assert.equal(calls.length,1);assert.equal(calls[0][0],'/payroll/a%2Fb/confirm');assert.equal(refresh,1);assert.equal(b.disabled,false);
 accept=false;await runPayrollAction(b);assert.equal(calls.length,1);
 accept=true;fail=true;await runPayrollAction(b);assert.match(message,/mock failure/);assert.equal(b.disabled,false);
 fail=false;b.dataset.payrollAction='deliver';await runPayrollAction(b);assert.match(message,/delivery failure/);
 b.dataset.payrollAction='resend';await runPayrollAction(b);assert.equal(calls.at(-1)[0],'/payroll/a%2Fb/deliver?resend=true');
})();'''
    script = tmp_path / 'actions.cjs'
    script.write_text(source)
    result = subprocess.run(['node', str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_live_admin_entry_reaches_existing_forms():
    admin = (ROOT / 'app/static/apps/obys/admin.html').read_text()
    assert 'function peopleWorkLinksV41()' in admin
    assert '${g.label==="직원·급여"?peopleWorkLinksV41():""}' in admin
    assert admin.count('기존 관리화면에서 처리') == 0
    assert 'index.html?legacy=1' not in admin
    assert 'href="/static/apps/obys/clock.html"' in admin
    assert 'people_view=${view}' not in admin
    assert 'if(Number(error.status)===401){clearAuthAndOpenLogin();return}' in admin
    assert 'includes(peopleEntry)) setView(peopleEntry)' in HTML


@NEEDS_NODE
def test_payroll_renderer_state_permissions(tmp_path):
    source = HTML.split('    function renderPayroll() {')[1].split('    function approvedEmployeesForForm')[0]
    script = tmp_path / 'render.cjs'
    script.write_text('''const assert=require('node:assert/strict');let manager=true;
const queueEmployeePortalSync=()=>{},isEmployeeExperience=()=>!manager,canManageOnboarding=()=>manager;
const document={getElementById:()=>null};let employeePreview=null;
const rows=[{id:'draft',status:'draft'},{id:'confirmed',status:'confirmed'},{id:'paid',status:'paid'},{id:'old',status:'paid',superseded_by:'new'}];
const selectedMonth=()=>'',visiblePayrollStatements=()=>rows,escapeHtml=x=>String(x),formatMoney=x=>String(x),employmentTaxTypeLabels={};
const payrollStatusLabels={draft:['작성중','info'],confirmed:['확정','good'],paid:['지급완료','good']};
const els={payrollRows:{innerHTML:''}};
function renderPayroll() {''' + source + '''
renderPayroll();let html=els.payrollRows.innerHTML;
assert.match(html,/data-delete-payroll="draft"/);assert.doesNotMatch(html,/data-delete-payroll="paid"/);
assert.equal((html.match(/data-payroll-action="confirm"/g)||[]).length,1);
assert.equal((html.match(/data-payroll-action="pay"/g)||[]).length,1);
assert.equal((html.match(/data-payroll-action="deliver"/g)||[]).length,2);
manager=false;renderPayroll();html=els.payrollRows.innerHTML;
assert.doesNotMatch(html,/data-edit-payroll|data-delete-payroll|data-payroll-action="versions"/);
assert.match(html,/명세 PDF/);
''')
    result = subprocess.run(['node', str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

@NEEDS_NODE
def test_shared_form_guard_and_ordered_invite_copy(tmp_path):
    helper = HTML.split('    async function withPeopleForm(form, action) {')[1].split('    async function createEmployeeInvite')[0]
    source = """const assert=require('node:assert/strict');
async function withPeopleForm(form, action) {""" + helper + """
(async()=>{
 const button={disabled:false}, disabled={disabled:true};
 const form={dataset:{},querySelectorAll:()=>[button,disabled]};
 let release, calls=0;
 const first=withPeopleForm(form,async()=>{calls++;await new Promise(r=>release=r)});
 await withPeopleForm(form,async()=>{calls++});
 assert.equal(calls,1);assert.equal(button.disabled,true);
 release();await first;assert.equal(button.disabled,false);assert.equal(disabled.disabled,true);
 await assert.rejects(withPeopleForm(form,async()=>{throw Error('offline')}));
 assert.equal(button.disabled,false);assert.equal(form.dataset.peopleBusy,undefined);
})();"""
    script=tmp_path/'form.cjs'
    script.write_text(source)
    result=subprocess.run(['node',str(script)],capture_output=True,text=True)
    assert result.returncode == 0,result.stderr
    invite=HTML.split('    async function createEmployeeInvite(form) {')[1].split('    async function acceptEmployeeInvite')[0]
    assert invite.index('await copyText(') < invite.index('form.reset()')
    assert HTML.count('return withPeopleForm(form, async () => {') == 4


def test_all_admin_routes_expire_and_clock_uses_real_cookie_names():
    admin=(ROOT/'app/static/apps/obys/admin.html').read_text()
    assert 'response.status===401&&[' not in admin
    assert 'state.liveLoading=false;clearAuthAndOpenLogin();return' in admin
    clock=(ROOT/'app/static/apps/obys/clock.html').read_text()
    assert 'const cookieName of ["aads_token", "fb_access_token"]' in clock
    assert 'document.cookie = `${key}' not in clock

@NEEDS_NODE
def test_salary_overwrite_recalculates_components_and_preserves_total(tmp_path):
    block=HTML.split('    function applyRegularSalaryPreset(overwrite = false) {')[1].split('    function applyContractDefaults')[0]
    script=tmp_path/'salary.cjs'
    script.write_text('''const assert=require('node:assert/strict');
const field=value=>({value});
const form={contractType:field('regular'),wage:field('4200000'),mealProvision:field('cash_no_meal'),taxableAllowance:field('100000'),baseSalary:field('123'),nonTaxMealAllowance:field('55'),wageComposition:field('직접 입력')};
const els={contractForm:form},formatMoney=String,isBlankOrPlaceholder=x=>!x;
function applyRegularSalaryPreset(overwrite = false) {'''+block+'''
applyRegularSalaryPreset(false);assert.equal(form.baseSalary.value,'123');assert.equal(form.wageComposition.value,'직접 입력');
applyRegularSalaryPreset(true);assert.equal(form.wage.value,'4200000');assert.equal(form.baseSalary.value,'3900000');assert.equal(form.nonTaxMealAllowance.value,'200000');
assert.match(form.wageComposition.value,/4200000/);
form.wage.value='';applyRegularSalaryPreset(true);assert.equal(form.wage.value,'3000000');
''')
    result=subprocess.run(['node',str(script)],capture_output=True,text=True)
    assert result.returncode == 0,result.stderr


@NEEDS_NODE
def test_v41_people_filters_and_server_payroll_amounts(tmp_path):
    admin = (ROOT / 'app/static/apps/obys/admin.html').read_text()
    names = ['payrollMoneyV41', 'payrollDisplayV41', 'attendanceMetricsV41', 'filteredRows']
    functions = '\n'.join(next(line.strip() for line in reversed(admin.splitlines())
                              if line.strip().startswith(f'function {name}(')) for name in names)
    script = tmp_path / 'people-v41.cjs'
    script.write_text('''const assert=require('node:assert/strict');
const state={route:'payroll',peopleMonth:'2026-10',peopleEmployee:'직원 가',peopleBranch:'매장 가',selected:{},status:'전체 상태',search:''};
''' + functions + '''
const row=(month,name,branch)=>({'직원':name,'지점':branch,__live:{raw:{payroll_month:month}}});
const c={data:[row('2026-10','직원 가','매장 가'),row('2026-09','직원 가','매장 가'),row('2026-10','직원 나','매장 가'),row('2026-10','직원 가','매장 나')]};
assert.equal(filteredRows(c).length,1);
state.route='home';assert.equal(filteredRows(c).length,4);
const display=payrollDisplayV41({raw:{gross_pay:3000000,net_pay:2700000,tax_withholding:100000,insurance_deduction:200000,status:'draft'}});
assert.equal(display.공제,'미집계');assert.equal(display.상태,'확정 전');
assert.equal(display.총지급액,'3,000,000원');assert.equal(display.실수령,'2,700,000원');
assert.equal(payrollDisplayV41({raw:{totals:{total_deductions:123456},status:'paid'}}).공제,'123,456원');
assert.equal(payrollDisplayV41({raw:{status:'confirmed'}}).상태,'확정');
assert.deepEqual(attendanceMetricsV41([{__live:{raw:{start_at:'09:00'}}}]),['출근|1건','지각|미집계','결근|미집계','초과|미집계']);
''')
    result = subprocess.run(['node', str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
