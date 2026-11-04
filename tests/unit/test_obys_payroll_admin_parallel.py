"""급여 관리자 핸들러를 합성 API/DOM으로 검증한다. 실직원 자료 접근 없음."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'app/static/apps/obys/modules/payroll-admin.js'


def test_payroll_handlers_save_readback_failure_permissions_and_confirmation():
    script = r'''
    const assert=require('node:assert/strict');global.window={};global.confirm=()=>true;
    require(process.argv[1]);
    const amounts=['gross_pay','taxable_pay','non_tax_meal_allowance','tax_withholding','insurance_deduction','other_deduction'];
    const controls=new Map();
    const element=key=>{if(!controls.has(key))controls.set(key,{value:'',textContent:'',innerHTML:'',style:{},disabled:false});return controls.get(key)};
    amounts.forEach(key=>{const control=element(key);let value='';Object.defineProperty(control,'value',{get:()=>value,set:next=>{value=String(next)}})});
    const form=element('form');form.elements=Object.fromEntries(amounts.map(k=>[k,element(k)]));form.reportValidity=()=>true;
    const actions=['defaults','draft','confirm','pdf','versions','pay','deliver'];
    actions.forEach(action=>element(`[data-payroll-action="${action}"]`).dataset={payrollAction:action});
    const host={isConnected:true,innerHTML:'',querySelector:element,
      querySelectorAll:selector=>selector==='[data-payroll-action]'?actions.map(a=>element(`[data-payroll-action="${a}"]`)):[...controls.values()]};
    let calls=[],writes=0,fail=false,requeryFail=false,confirmed=0,pdfs=0,afterSave=0;
    let row={id:'s1',business_id:'b1',employee_email:'one@example.test',payroll_month:'2026-11',status:'draft',version:1,
      gross_pay:1000,taxable_pay:1000,non_tax_meal_allowance:0,tax_withholding:0,insurance_deduction:0,other_deduction:0,net_pay:1000,totals:{total_deductions:0}};
    const api=async(path,options={})=>{
      calls.push(path);
      if(path.startsWith('/employees/approved'))return {employees:[{id:'e1',email:'one@example.test',name:'합성 직원',branch:'A'}]};
      if(path.startsWith('/payroll?')){if(requeryFail)throw Error('readback failed');return {statements:[row,{...row,id:'foreign',business_id:'b2'}]}}
      if(path==='/payroll'){
        writes++;if(fail)throw Error('save failed');
        const payload=JSON.parse(options.body);
        row={...row,...payload,id:row.status==='confirmed'?'s2':row.id,status:'draft',net_pay:payload.gross_pay,version:row.status==='confirmed'?2:1};
        return {statement:row};
      }
      if(path.startsWith('/payroll/defaults'))return {defaults:{gross_pay:null,taxable_pay:0},basis:'daily 일급 계약'};
      if(path==='/payroll/draft-from-attendance'){row={...row,gross_pay:2000,taxable_pay:2000,net_pay:2000,attendance_basis:{wage_type:'daily',approved_days:2,approved_minutes:960}};return {statement:row}}
      if(path.endsWith('/confirm')){confirmed++;row={...row,status:'confirmed'};return {statement:row}}
      if(path.endsWith('/versions'))return {versions:[row]};
      throw Error('unexpected API '+path);
    };
    const settle=()=>new Promise(resolve=>setImmediate(resolve));
    const click=async action=>{element(`[data-payroll-action="${action}"]`).onclick();await settle()};
    const submit=async()=>{form.onsubmit({preventDefault(){}});await settle()};
    (async()=>{
      await window.ObysPayrollAdmin.mount(host,{api,businessId:'b1',month:'2026-11'});
      assert.match(host.innerHTML,/권한/);assert.equal(calls.length,0);
      await window.ObysPayrollAdmin.mount(host,{api,businessId:'b1',month:'2026-11',canManage:true,onSaved:()=>afterSave++,downloadPdf:()=>pdfs++});
      assert.equal(form.elements.other_deduction.value,'0');
      assert.ok(!element('[data-payroll-statement]').innerHTML.includes('foreign'));
      form.elements.gross_pay.value='1500';form.oninput();fail=true;
      await submit();assert.equal(form.elements.gross_pay.value,'1500');assert.match(element('[data-payroll-message]').textContent,/save failed/);
      fail=false;requeryFail=true;await submit();assert.equal(form.elements.gross_pay.value,'1500');assert.equal(afterSave,0);
      requeryFail=false;
      const before=writes;form.onsubmit({preventDefault(){}});form.onsubmit({preventDefault(){}});await settle();
      assert.equal(writes,before+1);assert.equal(afterSave,1);assert.equal(row.gross_pay,1500);
      assert.match(element('[data-payroll-message]').textContent,/실수령 1,500원/);
      assert.equal(confirmed,0);await click('confirm');assert.equal(confirmed,1);assert.equal(row.status,'confirmed');
      const immutable=JSON.stringify(row);form.elements.gross_pay.value='1600';form.oninput();await submit();
      assert.equal(row.id,'s2');assert.equal(row.version,2);assert.equal(JSON.parse(immutable).gross_pay,1500);
      await click('pdf');assert.equal(pdfs,1);await click('versions');assert.match(element('[data-payroll-versions]').innerHTML,/버전/);
      await click('defaults');assert.equal(form.elements.gross_pay.value,'');assert.equal(form.elements.taxable_pay.value,'0');
      await click('draft');assert.equal(row.attendance_basis.wage_type,'daily');assert.equal(row.gross_pay,2000);
      assert.equal(calls.some(path=>/\/(pay|deliver)$/.test(path)),false);
    })().catch(e=>{console.error(e);process.exitCode=1});
    '''
    if not shutil.which("node"):
        pytest.skip("node 미설치 — 호스트에서 실행")
    result = subprocess.run(['node', '-e', script, str(MODULE)], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
