"""급여 공제 화면: 자동 재계산·수동 보존·늦은 응답 폐기·저장 페이로드·모바일 CSS(합성 API/DOM)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'app/static/apps/obys/modules/payroll-admin.js'

PREFIX = r'''
const assert=require('node:assert/strict');global.window={};global.confirm=()=>true;
require(process.argv[1]);
const amounts=['gross_pay','taxable_pay','non_tax_meal_allowance','tax_withholding','insurance_deduction','other_deduction'];
const controls=new Map();
const element=key=>{if(!controls.has(key))controls.set(key,{value:'',textContent:'',innerHTML:'',style:{},disabled:false});return controls.get(key)};
amounts.forEach(key=>{const c=element(key);let v='';Object.defineProperty(c,'value',{get:()=>v,set:n=>{v=String(n)}})});
const form=element('form');form.elements=Object.fromEntries(amounts.map(k=>[k,element(k)]));form.reportValidity=()=>true;
const actions=['defaults','draft','confirm','pdf','versions','pay','deliver'];
actions.forEach(a=>element(`[data-payroll-action="${a}"]`).dataset={payrollAction:a});
const host={isConnected:true,innerHTML:'',querySelector:element,
  querySelectorAll:s=>s==='[data-payroll-action]'?actions.map(a=>element(`[data-payroll-action="${a}"]`)):[...controls.values()]};
const auto=(code,label,amount,note)=>({code,label,amount,source:'auto',calc_note:note});
const row={id:'s1',business_id:'b1',employee_email:'one@example.test',payroll_month:'2026-11',status:'draft',version:1,
  gross_pay:2000000,taxable_pay:2000000,non_tax_meal_allowance:0,tax_withholding:0,insurance_deduction:0,other_deduction:0,net_pay:2000000,totals:{total_deductions:0},
  pay_items:[{code:'base',label:'기본급',amount:2000000,calc_note:''}],
  deduction_items:[auto('national_pension','국민연금',90000,'자동 A'),auto('health_insurance','건강보험',70000,'자동 B'),auto('long_term_care','노인장기요양보험',9000,'자동 C'),
    auto('employment_insurance','고용보험',18000,'자동 D'),auto('income_tax','소득세',0,'자동 E'),auto('local_income_tax','지방소득세',0,'자동 F')]};
let previews=[],saves=[],delay=0;
const api=async(path,options={})=>{
  if(path.startsWith('/employees/approved'))return {employees:[{id:'e1',email:'one@example.test',name:'합성 직원',branch:'A'},{id:'e2',email:'two@example.test',name:'다른 직원',branch:'A'}]};
  if(path.startsWith('/payroll?'))return {statements:[row]};
  if(path==='/payroll/deductions/preview'){
    const body=JSON.parse(options.body);previews.push(body);
    const wait=delay;delay=0;if(wait)await new Promise(r=>setTimeout(r,wait));
    const k=body.taxable_pay/1000;
    const items=body.deduction_items.map(i=>i.source==='manual'?{...i,needs_input:false}:{...i,amount:k,source:'auto',calc_note:'서버값 '+k});
    return {deduction_items:items,tax_withholding:0,insurance_deduction:0,other_deduction:0};
  }
  if(path==='/payroll'){saves.push(JSON.parse(options.body));return {statement:{...row,id:'s1'}};}
  throw Error('unexpected API '+path);
};
const settle=()=>new Promise(r=>setTimeout(r,450));
const itemsHost=element('[data-payroll-items]');
const type=(item,i,k,value,extra={})=>itemsHost.oninput({target:{dataset:{item,i:String(i),k},value,matches:()=>false,...extra}});
'''
SCRIPT = PREFIX + r'''
(async()=>{
  await window.ObysPayrollAdmin.mount(host,{api,businessId:'b1',month:'2026-11',canManage:true});
  await settle();
  // 로드 직후 계산식 입력값만 받아 오고 금액은 바꾸지 않는다.
  assert.equal(form.elements.insurance_deduction.value,'187000');
  // 1) 지급 금액 변경 → 디바운스 후 미리보기 1회, 자동 행만 새 값
  previews=[];
  type('pay',0,'amount','3000000');type('pay',0,'amount','3000000');
  assert.equal(previews.length,0);
  await settle();
  assert.equal(previews.length,1);assert.equal(previews[0].taxable_pay,3000000);assert.equal(previews[0].payroll_month,'2026-11');
  assert.equal(form.elements.insurance_deduction.value,String(3000*4));
  // 2) 건강보험 수동 입력 → source=manual 로 전송, 이후 지급 변경에도 덮어쓰지 않는다
  type('ded',1,'amount','50000');
  assert.equal(form.elements.insurance_deduction.value,String(3000*3+50000));  // 즉시 합계
  await settle();
  assert.equal(previews.at(-1).deduction_items[1].source,'manual');assert.equal(previews.at(-1).deduction_items[1].amount,50000);
  type('pay',0,'amount','4000000');await settle();
  assert.equal(form.elements.insurance_deduction.value,String(4000*3+50000));
  // 3) 0원 확인 체크
  type('ded',0,'amount','0');type('ded',0,'confirmed','on',{checked:true,type:'checkbox'});
  await settle();
  assert.equal(previews.at(-1).deduction_items[0].confirmed,true);
  // 4) 늦게 도착한 이전 응답은 버린다
  previews=[];delay=300;
  type('pay',0,'amount','5000000');
  await new Promise(r=>setTimeout(r,400));      // 첫 요청 발송됨(응답 대기 중)
  assert.equal(previews.length,1);
  type('pay',0,'amount','6000000');await new Promise(r=>setTimeout(r,900));
  assert.equal(previews.length,2);
  assert.equal(form.elements.insurance_deduction.value,String(6000*2+50000+0));  // 6000 기준 (국민연금 0 확인·건보 수동)
  // 5) 저장 페이로드에 source/confirmed/calc_note
  form.onsubmit({preventDefault(){}});await new Promise(r=>setTimeout(r,50));
  const ded=saves.at(-1).deduction_items;
  assert.equal(ded[0].source,'manual');assert.equal(ded[0].confirmed,true);
  assert.equal(ded[1].source,'manual');assert.equal(ded[1].amount,50000);assert.ok(ded[2].calc_note.includes('서버값'));assert.equal(ded[2].source,'auto');

  // 6) 리뷰 P2: 공제 메모 직접 입력은 row.calc_note 에 남고 입력칸이 되돌려지지 않는다
  type('ded',1,'calc_note','노무사 확인');
  type('pay',0,'amount','4100000');await settle();
  assert.equal(previews.at(-1).deduction_items[1].note_manual,true);
  assert.equal(previews.at(-1).deduction_items[1].calc_note,'노무사 확인');
  // 7) 리뷰 P2: 부양가족·근로시간 편집값이 미리보기 본문에 실린다
  previews=[];
  itemsHost.oninput({target:{dataset:{familyField:'1'},value:'3',matches:()=>false}});
  await settle();
  assert.equal(previews.at(-1).family,3);
  // CTO 판정: 근로시간(분)을 고쳐도 소정근로시간(monthly_scheduled_hours)은 화면이 보내지 않는다. 서버가 계약에서 읽는다.
  previews=[];
  itemsHost.oninput({target:{dataset:{workField:'work_minutes'},value:'2700',matches:()=>false}});
  await settle();
  assert.equal(previews.length,0);
  type('pay',0,'amount','4200000');await settle();
  assert.ok(previews.length>=1);previews.forEach(body=>assert.ok(!('monthly_scheduled_hours' in body)));
  // 8) 리뷰 P1: 응답 전에 저장하면 미리보기를 기다린 뒤 최신 값으로 저장한다
  previews=[];saves.length=0;delay=300;
  type('pay',0,'amount','7000000');
  form.onsubmit({preventDefault(){}});            // 디바운스 중에 저장
  await new Promise(r=>setTimeout(r,900));
  assert.equal(previews[0].taxable_pay,7000000);assert.equal(saves.length,1);
  assert.equal(saves.at(-1).deduction_items[2].amount,7000);
  console.log('OK');
})().catch(e=>{console.error(e);process.exit(1)});
'''


SCENARIOS = r'''
const FORMULA = {base:2000000, rate_pct:4.5};
row.insurance_deduction = 187000;
row.deduction_items[0] = {...row.deduction_items[0], formula:{...FORMULA}};
row.deduction_items[1] = {code:'health_insurance',label:'건강보험',amount:70000,source:'manual',calc_note:'기존 메모'};
const holdResponse = {fn:null};
const baseApi = api;
(async()=>{
  const wrapped = async (path, options) => {
    if (path === '/payroll/deductions/preview') {
      const body = JSON.parse(options.body), result = await baseApi(path, options);
      result.deduction_items = result.deduction_items.map((item, i) => body.deduction_items[i].formula && body.deduction_items[i].formula.edited
        ? {...item, amount:Math.round(body.deduction_items[i].formula.base * body.deduction_items[i].formula.rate_pct / 100), formula:body.deduction_items[i].formula}
        : (item.code === 'national_pension' ? {...item, formula:{...FORMULA}} : item));
      return result;
    }
    return baseApi(path, options);
  };
  await window.ObysPayrollAdmin.mount(host,{api:wrapped,businessId:'b1',month:'2026-11',canManage:true});
  await settle();
  const confirm = element('[data-payroll-action="confirm"]');
  assert.equal(confirm.disabled,false);
  // 지적 4: 기존 수동 공제의 메모만 바꿔도 dirty → 확정 버튼 잠김
  type('ded',1,'calc_note','바뀐 메모');
  assert.equal(confirm.disabled,true);
  type('ded',1,'calc_note','기존 메모');
  assert.equal(confirm.disabled,false);
  // 지적 2: 계산식 수정 → 응답 전 금액 직접 입력 → 저장 페이로드는 직접 입력값
  previews.length=0;saves.length=0;delay=300;
  type('ded',0,'f_base','2740000');
  await new Promise(r=>setTimeout(r,400));        // 요청 발송, 응답 대기
  assert.equal(previews.length,1);
  type('ded',0,'amount','12345');
  await new Promise(r=>setTimeout(r,900));
  assert.ok(previews.at(-1).deduction_items[0].formula===undefined||previews.at(-1).deduction_items[0].formula.edited!==true);
  assert.equal(previews.at(-1).deduction_items[0].amount,12345);
  form.onsubmit({preventDefault(){}});await new Promise(r=>setTimeout(r,100));
  const saved=saves.at(-1).deduction_items[0];
  assert.equal(saved.amount,12345);
  // 지적 2 후반: 계산식을 다시 고치면 다시 edited=true
  previews.length=0;delay=0;
  type('ded',0,'f_rate','5');await settle();
  assert.equal(previews.at(-1).deduction_items[0].formula.edited,true);
  // 지적 3: 계산식 수정 → 응답 전 calc_note 수정 → 옛 응답 도착 → 메모 유지, 재요청 1회
  previews.length=0;delay=300;
  type('ded',0,'f_base','1000000');
  await new Promise(r=>setTimeout(r,400));
  assert.equal(previews.length,1);
  type('ded',0,'calc_note','관리자 메모');
  await new Promise(r=>setTimeout(r,900));
  assert.equal(previews.length,2);
  assert.equal(previews.at(-1).deduction_items[0].calc_note,'관리자 메모');
  saves.length=0;
  form.onsubmit({preventDefault(){}});await new Promise(r=>setTimeout(r,100));
  assert.equal(saves.at(-1).deduction_items[0].calc_note,'관리자 메모');
  // 지적 4: 부양가족 3 입력 → 다른 직원 → 복귀 → 3 유지
  itemsHost.oninput({target:{dataset:{familyField:'1'},value:'3',matches:()=>false}});
  const picker=element('[data-payroll-employee]');
  picker.value='e2';picker.onchange();await settle();
  picker.value='e1';picker.onchange();await settle();
  previews.length=0;
  type('pay',0,'amount','2100000');await settle();
  assert.equal(previews.at(-1).family,3);
  console.log('OK');
})().catch(e=>{console.error(e);process.exit(1)});
'''


@pytest.mark.skipif(shutil.which('node') is None, reason='node 없음')
def test_live_deduction_recalc_flow():
    result = subprocess.run(['node', '-e', SCRIPT, str(MODULE)], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'OK' in result.stdout


def test_deduction_table_has_calc_method_column_and_manual_controls():
    js = MODULE.read_text(encoding='utf-8')
    assert '<th>계산방법</th>' in js and 'data-ded-revert' in js and '자동값으로 되돌리기' in js and '0원 확인' in js
    assert 'source:isAutoRow(row)' in js and '/payroll/deductions/preview' in js


def test_mobile_css_keeps_new_controls_44px_and_does_not_touch_settlement_rows():
    js = MODULE.read_text(encoding='utf-8')
    css = re.search(r'ITEM_CSS = `(.*?)`;', js, re.S).group(1)
    assert 'td.pi-note button' in css and 'label.pi-chk{min-height:44px' in css
    assert 'data-settlement-row' not in css and 'data-held-line' not in css


@pytest.mark.skipif(shutil.which('node') is None, reason='node 없음')
def test_edit_revision_dirty_and_family_scenarios():
    """계산식→금액 직접 입력 보존, 대기 중 calc_note 보존, 메모 dirty, 부양가족 유지, 근로시간 미전송."""
    result = subprocess.run(['node', '-e', PREFIX + SCENARIOS, str(MODULE)], capture_output=True, text=True, timeout=40, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'OK' in result.stdout


DELETE_SCRIPT = r'''
(async()=>{
  await window.ObysPayrollAdmin.mount(host,{api,businessId:'b1',month:'2026-11',canManage:true});
  await settle();
  // 수동 공제 행(기타)을 추가해 둔다. 응답 대기 중 이 행을 삭제하면 옛 응답이 되살리면 안 된다.
  itemsHost.onclick({target:{dataset:{itemAdd:'ded'}}});
  await settle();
  type('ded',6,'label','임시 공제');type('ded',6,'amount','7777');
  await settle();
  assert.ok(form.elements.insurance_deduction.value!==undefined);
  previews.length=0;delay=300;
  type('pay',0,'amount','3000000');
  await new Promise(r=>setTimeout(r,400));          // 요청 발송, 응답 대기
  assert.equal(previews.length,1);
  assert.ok(previews[0].deduction_items.some(i=>i.amount===7777));
  itemsHost.onclick({target:{dataset:{itemDel:'ded',i:'6'}}});   // 대기 중 삭제
  await new Promise(r=>setTimeout(r,900));          // 옛 응답 도착 + 재예약 요청
  assert.equal(previews.length,2);
  assert.ok(!previews.at(-1).deduction_items.some(i=>i.amount===7777));
  const total=Number(form.elements.other_deduction.value)+Number(form.elements.insurance_deduction.value)+Number(form.elements.tax_withholding.value);
  assert.ok(total<7777*2+1000000);
  form.onsubmit({preventDefault(){}});await new Promise(r=>setTimeout(r,100));
  assert.ok(!saves.at(-1).deduction_items.some(i=>i.amount===7777));
  console.log('OK');
})().catch(e=>{console.error(e);process.exit(1)});
'''


@pytest.mark.skipif(shutil.which('node') is None, reason='node 없음')
def test_deleting_manual_deduction_row_while_preview_in_flight_is_not_restored():
    result = subprocess.run(['node', '-e', PREFIX + DELETE_SCRIPT, str(MODULE)], capture_output=True, text=True, timeout=40, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'OK' in result.stdout


SETTLE_ALIGN_SCRIPT = r'''
(async()=>{
  const settle1={code:'prior_month_overpaid_recover',label:'전월 차액 정산',amount:5000,settlement_of_root_id:'r1',calc_note:'메모'};
  row.deduction_items=[...row.deduction_items,settle1];
  row.pay_items=[...row.pay_items,{code:'prior_month_topup',label:'전월 차액 추가 정산',amount:7000,settlement_of_root_id:'r2',calc_note:'지급메모'},
    {code:'held_release',label:'보류금',amount:3000,calc_note:'보류메모'}];
  await window.ObysPayrollAdmin.mount(host,{api,businessId:'b1',month:'2026-11',canManage:true});
  await settle();
  const html=[host.innerHTML,...[...controls.values()].map(c=>c.innerHTML)].join('\n');
  const tables=html.match(/<table class="payroll-item-table">.*?<\/table>/gs);
  assert.equal(tables.length,2);
  const [payT,dedT]=tables;
  const cols=tr=>[...tr.matchAll(/<td(?:\s[^>]*)?>/g)].reduce((n,m)=>n+Number((m[0].match(/colspan="(\d+)"/)||[0,1])[1]),0);
  const head=(dedT.match(/<thead>.*?<\/thead>/s)[0].match(/<th>/g)||[]).length;
  assert.equal(head,5);
  const rows=dedT.match(/<tr[ >].*?<\/tr>/gs).filter(r=>!r.includes("<th>"));
  assert.ok(rows.some(r=>r.includes('data-settlement-row')));
  for(const r of rows)assert.equal(cols(r),5);
  const sr=rows.find(r=>r.includes('data-settlement-row'));
  const tds=sr.match(/<td.*?<\/td>/gs);
  assert.ok(tds[4].includes('data-settle-off="r1"')&&tds[4].includes('이번 달 제외')&&tds[4].includes('min-height:44px'));
  assert.equal(tds[3],'<td></td>');
  const S=window.ObysPayrollAdmin.settlementRowHtml,H=window.ObysPayrollAdmin.heldRowHtml;
  const r0={label:'전월 차액 정산',amount:5000,settlement_of_root_id:'r1',calc_note:'메모'};
  assert.equal(S('pay',r0),'<tr data-settlement-row>\n      <td>전월 차액 정산</td><td>전월 차액 정산</td><td>5,000</td><td>메모</td>\n      <td><button type="button" class="btn" style="min-height:44px" data-settle-off="r1">이번 달 제외</button></td></tr>');
  assert.equal(S('deduction',r0),'<tr data-settlement-row>\n      <td>전월 차액 정산</td><td>전월 차액 정산</td><td>5,000</td>\n      <td><button type="button" class="btn" style="min-height:44px" data-settle-off="r1">이번 달 제외</button></td></tr>');
  assert.equal(S('pay',r0,true),'<tr data-settlement-row>\n      <td>전월 차액 정산</td><td>전월 차액 정산</td><td>5,000</td><td>메모</td>\n      <td><small>지급 완료 사실</small></td></tr>');
  assert.equal(H({label:'보류금',amount:3000,calc_note:'보류메모'}),'<tr data-held-line>\n      <td>보류금 지급</td><td>보류금</td><td>3,000</td><td>보류메모</td><td><small>서버 계산 · 수정 불가</small></td></tr>');
  // 지급 표의 정산·보류 행은 기존 함수 출력과 같다.
  assert.ok(payT.includes(S('pay',{label:'전월 차액 추가 정산',amount:7000,settlement_of_root_id:'r2',calc_note:'지급메모'})));
  assert.ok(payT.includes(H({label:'보류금',amount:3000,calc_note:'보류메모'})));
  console.log('OK');
})().catch(e=>{console.error(e);process.exit(1)});
'''


@pytest.mark.skipif(shutil.which('node') is None, reason='node 없음')
def test_deduction_settlement_row_matches_header_columns_and_render_functions_unchanged():
    result = subprocess.run(['node', '-e', PREFIX + SETTLE_ALIGN_SCRIPT, str(MODULE)], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'OK' in result.stdout
