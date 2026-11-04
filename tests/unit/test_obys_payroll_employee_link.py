"""오비서 관리자 급여 현황 ② — 목록 행 → 승인 직원 연결(employee_key 해시 대조). node 로 실제 함수를 실행한다."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / "app/static/apps/obys/admin.html"
MODULE = ROOT / "app/static/apps/obys/modules/payroll-admin.js"
NODE = shutil.which("node") or "/usr/bin/node"
pytestmark = pytest.mark.skipif(not Path(NODE).exists(), reason="node 없음")

BIZ = "열정 본점"


def key_of(email: str, business: str = BIZ) -> str:
    """서버 obys_workspaces._employee_key 와 같은 식(독립 계산)."""
    return hashlib.sha256((business + ":" + email.strip().lower()).encode()).hexdigest()


def run_node(script: str, data: dict | None = None):
    prelude = (
        "const assert=require('node:assert/strict');global.window={};global.confirm=()=>true;"
        "if(!globalThis.crypto)globalThis.crypto=require('node:crypto').webcrypto;"
        "const DATA=JSON.parse(process.argv[2]);require(process.argv[1]);const P=window.ObysPayrollAdmin;"
        "const settle=()=>new Promise(r=>setImmediate(r));\n"
    )
    result = subprocess.run(
        [NODE, "-e", prelude + script, str(MODULE), json.dumps(data or {})],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


FAKE_DOM = r"""
const AMOUNTS=['gross_pay','taxable_pay','non_tax_meal_allowance','tax_withholding','insurance_deduction','other_deduction'];
const ACTIONS=['defaults','draft','settlement','confirm','pdf','versions','pay','deliver'];
function makeHost(){
  const controls=new Map();
  const element=k=>{if(!controls.has(k))controls.set(k,{value:'',textContent:'',innerHTML:'',style:{},disabled:false});return controls.get(k)};
  AMOUNTS.forEach(k=>{const c=element(k);let v='';Object.defineProperty(c,'value',{get:()=>v,set:n=>{v=String(n)}})});
  const form=element('form');form.elements=Object.fromEntries(AMOUNTS.map(k=>[k,element(k)]));form.reportValidity=()=>true;
  ACTIONS.forEach(a=>{element(`[data-payroll-action="${a}"]`).dataset={payrollAction:a}});
  const submit=element('submit-button');
  const host={isConnected:true,innerHTML:'',querySelector:element,
    querySelectorAll:s=>s==='[data-payroll-action]'?ACTIONS.map(a=>element(`[data-payroll-action="${a}"]`)):[...controls.values()]};
  return {host,element,form,submit,picker:element('[data-payroll-employee]'),branch:element('[data-payroll-branch]')};
}
function makeApi(employees,statements=[]){
  const posts=[],calls=[];let stmts=statements;
  const api=async(path,options={})=>{
    calls.push(path);
    if(path.startsWith('/employees/approved'))return {employees};
    if(path.startsWith('/payroll?'))return {statements:stmts};
    if(path==='/payroll'){
      const p=JSON.parse(options.body);posts.push(p);
      const row={...p,id:'saved-'+posts.length,business_id:'b1',payroll_month:'2026-11',status:'draft',version:1,net_pay:p.gross_pay,totals:{total_deductions:0}};
      stmts=[row,...stmts];return {statement:row};
    }
    throw Error('unexpected API '+path);
  };
  return {api,posts,calls};
}
const submitForm=async d=>{AMOUNTS.forEach(k=>{d.form.elements[k].value=k==='gross_pay'||k==='taxable_pay'?'1000':'0'});d.form.oninput();d.form.onsubmit({preventDefault(){}});await settle()};
const person=(id,email,extra={})=>({id,email,name:'직원'+id,branch:'A',...extra});
"""


def run_dom(script: str, data: dict | None = None):
    return run_node(FAKE_DOM + "(async()=>{" + script + "})().catch(e=>{console.error(e);process.exit(1)})", data)


# ───────────────────────── 연결 함수 ─────────────────────────

def test_t1_employee_key_matches_one_approved_employee():
    out = run_node(
        """(async()=>{
const employees=[{id:'id-a',email:'A@Shop.test '},{id:'id-b',email:'b@shop.test'}];
const r=await P.resolvePayrollEmployee({employeeKey:DATA.key,employees,businessName:DATA.biz});
console.log(JSON.stringify({status:r.status,id:r.employee&&r.employee.id}))})()""",
        {"key": key_of("a@shop.test"), "biz": BIZ},
    )
    assert out == {"status": "matched", "id": "id-a"}


def test_t2_same_email_twice_is_ambiguous_and_editor_is_blocked():
    out = run_dom(
        """
const employees=[person('e1','dup@shop.test'),person('e2','dup@shop.test'),person('e3','z@shop.test')];
const r=await P.resolvePayrollEmployee({employeeKey:DATA.key,employees,businessName:DATA.biz});
const d=makeHost(),{api,posts}=makeApi(employees);
await P.mount(d.host,{api,businessId:'b1',month:'2026-11',canManage:true,requireSelection:r.status!=='matched',linkStatus:r.status});
await submitForm(d);
console.log(JSON.stringify({status:r.status,id:r.employee,n:r.candidates.length,posts:posts.length,saveDisabled:d.submit.disabled,
 grossDisabled:d.form.elements.gross_pay.disabled,confirmDisabled:d.element('[data-payroll-action="confirm"]').disabled,
 payDisabled:d.element('[data-payroll-action="pay"]').disabled,deliverDisabled:d.element('[data-payroll-action="deliver"]').disabled,
 pickerDisabled:d.picker.disabled,pickerValue:d.picker.value,notice:d.element('[data-payroll-blocked]').textContent}))""",
        {"key": key_of("dup@shop.test"), "biz": BIZ},
    )
    assert out["status"] == "ambiguous" and out["id"] is None and out["n"] == 2
    assert out["posts"] == 0
    assert out["saveDisabled"] and out["grossDisabled"] and out["confirmDisabled"] and out["payDisabled"] and out["deliverDisabled"]
    assert out["pickerDisabled"] is False and out["pickerValue"] == ""
    assert "이 명세의 직원을 승인 직원 목록에서 찾지 못했습니다. 아래에서 직원을 직접 선택하세요." in out["notice"]


def test_t3_departed_employee_is_not_linked_by_name():
    out = run_node(
        """(async()=>{
const employees=[{id:'new-id',email:'new@shop.test',name:'김철수'}];
const r=await P.resolvePayrollEmployee({employeeKey:DATA.key,employees,businessName:DATA.biz});
console.log(JSON.stringify({status:r.status,employee:r.employee}))})()""",
        {"key": key_of("old@shop.test"), "biz": BIZ},
    )
    assert out == {"status": "unmatched", "employee": None}


def test_missing_employee_key_is_unmatched_and_other_business_does_not_match():
    out = run_node(
        """(async()=>{
const employees=[{id:'id-a',email:'a@shop.test'}];
const none=await P.resolvePayrollEmployee({employeeKey:'',employees,businessName:DATA.biz});
const other=await P.resolvePayrollEmployee({employeeKey:DATA.key,employees,businessName:'다른 사업자'});
console.log(JSON.stringify([none.status,other.status]))})()""",
        {"key": key_of("a@shop.test"), "biz": BIZ},
    )
    assert out == ["unmatched", "unmatched"]


def test_t8_masked_email_is_never_used_for_matching_or_ownership():
    out = run_node(
        """(async()=>{
const masked=[{id:'m1',email:'[MASKED]'},{id:'m2',email:'a***@shop.test'}];
const viaMasked=await P.resolvePayrollEmployee({employeeKey:DATA.maskedKey,employees:masked,businessName:DATA.biz});
const real={id:'e1',email:'a@shop.test'};
const own=P.ownsStatement({employee_email:'[MASKED]',payroll_month:'2026-11'},real,[real]);
const ownStars=P.ownsStatement({employee_email:'a***@shop.test'},real,[real]);
console.log(JSON.stringify({viaMasked:viaMasked.status,own,ownStars}))})()""",
        {"maskedKey": key_of("[MASKED]"), "biz": BIZ},
    )
    assert out == {"viaMasked": "unmatched", "own": False, "ownStars": False}


# ───────────────────────── 명세 소속 ─────────────────────────

def test_t6_same_email_without_employee_request_id_belongs_to_neither():
    out = run_dom(
        """
const e1=person('e1','dup@shop.test'),e2=person('e2','dup@shop.test'),all=[e1,e2];
const st={id:'s1',employee_email:'dup@shop.test',payroll_month:'2026-11'};
const solo=person('s','solo@shop.test');
console.log(JSON.stringify({e1:P.ownsStatement(st,e1,all),e2:P.ownsStatement(st,e2,all),kind:P.statementOwnership(st,e1,all),
 solo:P.ownsStatement({employee_email:'SOLO@shop.test '},solo,[solo,e1,e2])}))""")
    assert out == {"e1": False, "e2": False, "kind": "ambiguous", "solo": True}


def test_t6_editor_shows_ambiguous_statement_notice_and_hides_the_statement():
    out = run_dom(
        """
const employees=[person('e1','dup@shop.test'),person('e2','dup@shop.test')];
const d=makeHost(),{api}=makeApi(employees,[{id:'s1',business_id:'b1',employee_email:'dup@shop.test',payroll_month:'2026-11',status:'draft',version:1}]);
await P.mount(d.host,{api,businessId:'b1',month:'2026-11',canManage:true,selectedEmployeeId:'e1'});
console.log(JSON.stringify({notice:d.element('[data-payroll-link-notice]').textContent,statements:d.element('[data-payroll-statement]').innerHTML}))""")
    assert "같은 이메일 직원이 여럿이라 명세를 자동 연결하지 않았습니다" in out["notice"]
    assert "s1" not in out["statements"]


def test_t7_statement_with_employee_request_id_belongs_only_to_that_employee():
    out = run_dom(
        """
const e1=person('e1','dup@shop.test'),e2=person('e2','dup@shop.test'),all=[e1,e2];
const st={id:'s2',employee_email:'dup@shop.test',employee_request_id:'e2'};
const nested={id:'s3',employee_email:'dup@shop.test',statement_payload:{employee_request_id:'e2'}};
console.log(JSON.stringify({e1:P.ownsStatement(st,e1,all),e2:P.ownsStatement(st,e2,all),nested1:P.ownsStatement(nested,e1,all),nested2:P.ownsStatement(nested,e2,all)}))""")
    assert out == {"e1": False, "e2": True, "nested1": False, "nested2": True}


# ───────────────────────── 편집기 선택 ─────────────────────────

def test_t4_departed_id_with_same_email_new_employee_is_blocked_not_rescued():
    out = run_dom(
        """
const employees=[person('new-id','same@shop.test')];
const d=makeHost(),{api,posts}=makeApi(employees);const picked=[];
await P.mount(d.host,{api,businessId:'b1',month:'2026-11',canManage:true,selectedEmployeeId:'departed-id',onSelect:id=>picked.push(id)});
await submitForm(d);
console.log(JSON.stringify({value:d.picker.value,saveDisabled:d.submit.disabled,posts:posts.length,picked,notice:d.element('[data-payroll-blocked]').textContent,
 hasPlaceholder:d.picker.innerHTML.includes('직원을 선택하세요')}))""")
    assert out["value"] == "" and out["saveDisabled"] and out["posts"] == 0 and out["picked"] == []
    assert out["hasPlaceholder"] and "찾지 못했습니다" in out["notice"]


def test_blocked_editor_opens_after_user_chooses_an_employee():
    out = run_dom(
        """
const employees=[person('e1','a@shop.test'),person('e2','b@shop.test')];
const d=makeHost(),{api,posts}=makeApi(employees);const picked=[];
await P.mount(d.host,{api,businessId:'b1',month:'2026-11',canManage:true,selectedEmployeeId:'gone',onSelect:id=>picked.push(id)});
const before=d.submit.disabled;
d.picker.value='e2';d.picker.onchange();await settle();
await submitForm(d);
console.log(JSON.stringify({before,after:d.submit.disabled,picked,posts}))""")
    assert out["before"] is True and out["after"] is False and out["picked"] == ["e2"]
    assert out["posts"][0]["employee_request_id"] == "e2" and out["posts"][0]["employee_email"] == "b@shop.test"


def test_no_selection_passed_opens_first_employee_by_default():
    out = run_dom(
        """
const employees=[person('e1','a@shop.test'),person('e2','b@shop.test')];
const d=makeHost(),{api}=makeApi(employees);
await P.mount(d.host,{api,businessId:'b1',month:'2026-11',canManage:true});
console.log(JSON.stringify({value:d.picker.value,saveDisabled:d.submit.disabled,optionIsId:/value="e1"/.test(d.picker.innerHTML)&&!/value="a@/.test(d.picker.innerHTML)}))""")
    assert out == {"value": "e1", "saveDisabled": False, "optionIsId": True}


def test_t5_save_payload_uses_the_selected_approved_id_even_when_emails_are_equal():
    out = run_dom(
        """
const employees=[person('e1','dup@shop.test'),person('e2','dup@shop.test')];
const d=makeHost(),{api,posts}=makeApi(employees);const picked=[];
await P.mount(d.host,{api,businessId:'b1',month:'2026-11',canManage:true,selectedEmployeeId:'e1',onSelect:id=>picked.push(id)});
d.picker.value='e2';d.picker.onchange();await settle();
await submitForm(d);
console.log(JSON.stringify({picked,ids:posts.map(p=>p.employee_request_id),value:d.picker.value}))""")
    assert out == {"picked": ["e2"], "ids": ["e2"], "value": "e2"}


def test_t10_branch_filter_removing_selected_employee_does_not_fall_back_to_first():
    out = run_dom(
        """
const employees=[person('e1','a@shop.test',{branch:'A'}),person('e2','b@shop.test',{branch:'B'})];
const d=makeHost(),{api,posts}=makeApi(employees);const picked=[];
await P.mount(d.host,{api,businessId:'b1',month:'2026-11',canManage:true,selectedEmployeeId:'e2',onSelect:id=>picked.push(id)});
d.branch.value='A';d.branch.onchange({target:d.branch});await settle();
await submitForm(d);
const filtered={value:d.picker.value,picked:picked.slice(),saveDisabled:d.submit.disabled,posts:posts.length,notice:d.element('[data-payroll-blocked]').textContent};
d.branch.value='';d.branch.onchange({target:d.branch});await settle();
console.log(JSON.stringify({filtered,restoredValue:d.picker.value,restoredDisabled:d.submit.disabled}))""")
    assert out["filtered"]["value"] == "" and out["filtered"]["picked"] == []
    assert out["filtered"]["saveDisabled"] is True and out["filtered"]["posts"] == 0
    assert "선택한 직원이 이 지점 목록에 없습니다" in out["filtered"]["notice"]
    assert out["restoredValue"] == "e2" and out["restoredDisabled"] is False


# ───────────────────────── admin.html (행 클릭·상태·중복 조회) ─────────────────────────

def _fns(*names: str) -> str:
    src = HTML.read_text(encoding="utf-8")
    lines = []
    for name in names:
        # loadBusinesses 는 파일에 두 번 정의되어 있고 마지막 정의가 적용된다 → 마지막 한 줄 정의를 쓴다.
        found = re.findall(r"^    (?:async )?function " + name + r"\(.*$", src, re.M)
        assert found, name
        lines.append(found[-1])
    return "\n".join(lines)


LINK_FNS = ("payrollSetEmployeeV41", "payrollResetSelectionV41", "payrollLinkCacheV41", "payrollApprovedV41", "payrollResolveV41",
            "payrollLinksV41", "payrollRowNoteV41", "payrollSelectRowV41", "payrollEditorSelectionV41")


def run_page(scenario: str, data: dict | None = None):
    prelude = """
const state={businessId:'A',businessName:%s,payrollEmployeeId:'',payrollRequireSelection:false,payrollLinkStatus:'',payrollLinking:false,payrollClickSeq:0,payrollLinkCache:null};
const toasts=[];const showToast=m=>toasts.push(m);
const approvedCalls=[];let approvedGate=null;let approvedList=[];
const financeApiV41=async path=>{approvedCalls.push(path);if(approvedGate)await approvedGate;return {employees:approvedList}};
const row=(email,name)=>({__live:{id:'rec-'+email,employee_key:DATA.keys[email]||null,raw:{employee_email:'[MASKED]',employee_name:name}}});
""" % json.dumps(BIZ)
    return run_node(prelude + _fns(*LINK_FNS) + "\n(async()=>{" + scenario + "})().catch(e=>{console.error(e);process.exit(1)})", data)


def _keys(*emails: str) -> dict:
    return {"keys": {e: key_of(e) for e in emails}}


def test_row_click_links_matched_ambiguous_unmatched_and_exposes_labels():
    out = run_page(
        """
approvedList=[{id:'e1',email:'a@shop.test'},{id:'e2',email:'dup@shop.test'},{id:'e3',email:'dup@shop.test'}];
const rows=[row('a@shop.test','가'),row('dup@shop.test','나'),row('gone@shop.test','다'),{__live:{id:'x',raw:{employee_name:'라'}}}];
const res={};
await payrollLinksV41(rows);
res.notes=rows.map(payrollRowNoteV41);
for(const [i,r] of rows.entries()){await payrollSelectRowV41(r);res['row'+i]=payrollEditorSelectionV41()}
console.log(JSON.stringify(res))""", _keys("a@shop.test", "dup@shop.test", "gone@shop.test"))
    assert out["row0"] == {"linking": False, "selectedEmployeeId": "e1", "requireSelection": False, "linkStatus": "matched"}
    assert out["row1"] == {"linking": False, "selectedEmployeeId": "", "requireSelection": True, "linkStatus": "ambiguous"}
    assert out["row2"] == {"linking": False, "selectedEmployeeId": "", "requireSelection": True, "linkStatus": "unmatched"}
    assert out["row3"]["selectedEmployeeId"] == "" and out["row3"]["linkStatus"] == "unmatched"
    assert out["notes"][0] == ""
    assert "직원 확인 필요" in out["notes"][1]
    assert "직원 미연결" in out["notes"][2] and "직원 미연결" in out["notes"][3]


def test_review1_editor_is_blocked_while_linking_and_late_response_does_not_override_user_choice():
    out = run_page(
        """
approvedList=[{id:'e1',email:'a@shop.test'}];
let release;approvedGate=new Promise(r=>{release=r});
const pending=payrollSelectRowV41(row('a@shop.test','가'));
const during=payrollEditorSelectionV41();
payrollSetEmployeeV41('e9',false);
release();const applied=await pending;
console.log(JSON.stringify({during,applied,id:state.payrollEmployeeId,linking:state.payrollLinking,after:payrollEditorSelectionV41()}))""", _keys("a@shop.test"))
    assert out["during"] == {"linking": True}
    assert out["applied"] is False and out["id"] == "e9" and out["linking"] is False
    assert out["after"]["selectedEmployeeId"] == "e9"


def test_review1_second_row_click_supersedes_the_first_slow_one():
    out = run_page(
        """
approvedList=[{id:'e1',email:'a@shop.test'},{id:'e2',email:'b@shop.test'}];
let release;approvedGate=new Promise(r=>{release=r});
const first=payrollSelectRowV41(row('a@shop.test','가'));
const second=payrollSelectRowV41(row('b@shop.test','나'));
release();const [a,b]=await Promise.all([first,second]);
console.log(JSON.stringify({a,b,id:state.payrollEmployeeId,status:state.payrollLinkStatus}))""", _keys("a@shop.test", "b@shop.test"))
    assert out == {"a": False, "b": True, "id": "e2", "status": "matched"}


def test_review2_many_rows_share_one_approved_employee_request():
    out = run_page(
        """
approvedList=['a','b','c'].map((n,i)=>({id:'e'+i,email:n+'@shop.test'}));
let release;approvedGate=new Promise(r=>{release=r});
const rows=['a','b','c'].map(n=>row(n+'@shop.test',n));
const batch=[payrollLinksV41(rows),payrollLinksV41(rows),...rows.map(r=>payrollSelectRowV41(r))];
release();await Promise.all(batch);
await payrollLinksV41(rows);
console.log(JSON.stringify({calls:approvedCalls.length,statuses:rows.map(r=>state.payrollLinkCache.links[r.__live.employee_key].status)}))""",
        _keys("a@shop.test", "b@shop.test", "c@shop.test"))
    assert out == {"calls": 1, "statuses": ["matched", "matched", "matched"]}


def test_failed_approved_lookup_is_not_cached_and_click_retries_and_never_picks_a_default():
    out = run_page(
        """
approvedList=[{id:'e1',email:'a@shop.test'}];
const r0=row('a@shop.test','가');
approvedGate=Promise.reject(new Error('조회 실패'));approvedGate.catch(()=>{});
await payrollSelectRowV41(r0);
const afterFail={id:state.payrollEmployeeId,status:state.payrollLinkStatus,linking:state.payrollLinking,toast:toasts.length};
approvedGate=null;
await payrollSelectRowV41(r0);
console.log(JSON.stringify({afterFail,id:state.payrollEmployeeId,calls:approvedCalls.length}))""", _keys("a@shop.test"))
    assert out["afterFail"] == {"id": "", "status": "error", "linking": False, "toast": 1}
    assert out["id"] == "e1" and out["calls"] == 2


def test_rows_without_employee_key_are_unmatched_without_any_request():
    out = run_page(
        """
const r={__live:{id:'x',raw:{employee_name:'라'}}};
await payrollSelectRowV41(r);
console.log(JSON.stringify({calls:approvedCalls.length,sel:payrollEditorSelectionV41(),note:payrollRowNoteV41(r)}))""", {"keys": {}})
    assert out["calls"] == 0 and out["sel"]["requireSelection"] is True and out["sel"]["selectedEmployeeId"] == ""
    assert "직원 미연결" in out["note"]


def test_t9_business_switch_clears_payroll_selection_after_a_row_click():
    src = HTML.read_text(encoding="utf-8")
    fns = _fns("payrollSetEmployeeV41", "payrollResetSelectionV41", "loadBusinesses")
    script = """
const state={businessId:'A',businessName:'가',businesses:[],payrollEmployeeId:'e1',payrollRequireSelection:true,payrollLinkStatus:'matched',payrollLinking:true,payrollClickSeq:3,payrollLinkCache:{x:1},recordOffset:5,selected:{}};
const els={};const document={getElementById:id=>els[id]||(els[id]={textContent:'',innerHTML:'',hidden:false})};
const localStorage={getItem:()=>null,setItem(){}};const escapeHtml=s=>String(s);let loads=0;const loadLiveRoute=()=>{loads++};const renderPageV41=()=>{};
const workspaceApi=async()=>({businesses:[{id:'A',name:'가'},{id:'B',name:'나'}]});
""" + fns + """
(async()=>{await loadBusinesses();
 const before={id:state.payrollEmployeeId,req:state.payrollRequireSelection,seq:state.payrollClickSeq};
 els.businessSelect.value='B';els.businessSelect.onchange();
 console.log(JSON.stringify({before,biz:state.businessId,id:state.payrollEmployeeId,req:state.payrollRequireSelection,status:state.payrollLinkStatus,linking:state.payrollLinking,seq:state.payrollClickSeq,cache:state.payrollLinkCache,loads}))})()
"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["biz"] == "B" and got["id"] == "" and got["req"] is False and got["status"] == "" and got["linking"] is False
    assert got["cache"] is None and got["seq"] > got["before"]["seq"] and got["loads"] == 1
    assert "payrollEmployee:" not in src and "state.payrollEmployee=" not in src


def test_t9_organization_switch_clears_payroll_selection_before_reload():
    fns = _fns("payrollSetEmployeeV41", "payrollResetSelectionV41", "loadOrganizations")
    script = """
const state={payrollEmployeeId:'e1',payrollRequireSelection:true,payrollLinkStatus:'matched',payrollLinking:true,payrollClickSeq:3,payrollLinkCache:{x:1}};
const els={};const document={cookie:'',getElementById:id=>els[id]||(els[id]={innerHTML:'',value:'T2'})};
const store={};const localStorage={setItem:(k,v)=>{store[k]=v},removeItem:k=>{delete store[k]}};let reloads=0;const location={reload(){reloads++}};
const escapeHtml=s=>String(s);const showToast=()=>{};
const organizationApi=async path=>path==='/tenants'?{tenants:[{tenant_id:'T1',name:'가'},{tenant_id:'T2',name:'나'}],current_tenant_id:'T1'}:{token:'tok'};
""" + fns + """
(async()=>{await loadOrganizations();await els.organizationSelect.onchange();
 console.log(JSON.stringify({id:state.payrollEmployeeId,req:state.payrollRequireSelection,linking:state.payrollLinking,cache:state.payrollLinkCache,reloads}))})()
"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"id": "", "req": False, "linking": False, "cache": None, "reloads": 1}


def test_month_change_keeps_the_selected_employee_in_source():
    src = HTML.read_text(encoding="utf-8")
    m = re.search(r'document\.getElementById\("peopleMonth"\)\.onchange=e=>\{.*', src)
    assert m and "payrollSetEmployeeV41" not in m.group(0) and "payrollResetSelectionV41" not in m.group(0)


def test_render_wiring_in_source():
    src = HTML.read_text(encoding="utf-8")
    assert "selectedEmployeeId:sel.selectedEmployeeId,requireSelection:sel.requireSelection,linkStatus:sel.linkStatus,onSelect:id=>payrollSetEmployeeV41(id,!id)" in src
    assert "selectedEmail" not in src
    assert "if(sel.linking)document.getElementById(\"payrollEditor\").innerHTML=" in src
    assert 'payrollRowNoteV41(row):""' in src
    assert "payrollSelectRowV41(inMonth.find(r=>identity(r)===b.dataset.person))" in src
    js = MODULE.read_text(encoding="utf-8")
    assert "selectedEmail" not in js and "employee_email:selected" not in js
