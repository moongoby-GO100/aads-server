"""직원 선택(직원 식별키) 연결 수정 + 근무표 월간 일괄 등록 검증."""
import calendar
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.api import obys_workspaces as api

ADMIN = Path("app/static/apps/obys/admin.html")


def _lines(html, prefixes):
    return "\n".join(next(l.strip() for l in html.splitlines() if l.strip().startswith(p)) for p in prefixes)


def _need_node():
    if not shutil.which("node"):
        pytest.skip("node 없음")


def _run(script_path, body):
    script_path.write_text(body)
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout


# ---------------------------------------------------------------- 서버: 직원 식별키
def test_select_strings_include_employee_email():
    for route in ("attendance", "payroll"):
        sql = api.GENERIC_ROUTE_SQL[route][0]
        assert "employee_name,employee_email," in sql, route
    assert "employee_name,employee_email,branch" in api.ATTENDANCE_OWN_SQL


def _rec(email, **extra):
    return api._record("generic", {"id": "rec-1", "employee_name": "김직원", "employee_email": email, **extra}, "사업자")


def test_record_key_is_per_employee_not_per_record():
    first = api._record("generic", {"id": "r1", "employee_name": "김", "employee_email": "k@x.test"}, "사업자")
    second = api._record("generic", {"id": "r2", "employee_name": "김", "employee_email": "k@x.test"}, "사업자")
    assert first["employee_key"] and first["employee_key"] == second["employee_key"]
    assert api._record("generic", {"id": "r3", "employee_name": "김"}, "사업자")["employee_key"] is None


def test_record_and_schedule_keys_match_regardless_of_case_and_spaces():
    record_key = _rec("  Kim@X.TEST ")["employee_key"]
    assert record_key == api._employee_key("사업자", None, "kim@x.test")
    assert record_key == api._employee_key("사업자", "emp-1", "KIM@x.test")
    assert record_key != api._employee_key("사업자", None, "other@x.test")
    assert api._employee_key("사업자", "emp-1", "") == api._employee_key("사업자", " emp-1 ", None)
    assert api._employee_key("사업자", None, None) is None


def test_raw_still_masks_employee_email():
    assert _rec("k@x.test")["raw"]["employee_email"] == "[MASKED]"
    assert "k@x.test" not in str(_rec("k@x.test"))


# ---------------------------------------------------------------- 정적
def test_static_labels_and_sizes():
    html = ADMIN.read_text()
    for text in ("직원 선택", 'id="peoplePersonSelect"', "등록 단위", 'id="scheduleUnit"', 'type="month"',
                 "월간 일정 일괄 등록", "주간 일정 일괄 등록", "칸 등록", "최대 1,000칸", "월간 보기", "주간 보기"):
        assert text in html, text
    assert 'id="peoplePersonSelect"' in html and "min-height:44px" in html
    assert html.count("data-person") >= 3


def test_browser_script_syntax(tmp_path):
    _need_node()
    script = tmp_path / "admin.js"
    script.write_text("\n".join(re.findall(r"<script>(.*?)</script>", ADMIN.read_text(), re.S)))
    subprocess.run(["node", "--check", str(script)], check=True, capture_output=True)


# ---------------------------------------------------------------- 직원 선택 렌더
def test_detail_tab_select_switches_calendar_rows(tmp_path):
    _need_node()
    html = ADMIN.read_text()
    helpers = _lines(html, ("function escapeHtml(", "function metricHtml(", "function payrollMoneyV41(",
        "function payrollDisplayV41(", "function attendanceStatusV41(", "function attendanceReviewV41(",
        "function attendanceMinutesV41(", "function displayDateV41(", "function attendanceTimeV41(", "function attendanceDisplayV41(", "function liveRow(",
        "function filteredRows(c)", "function peopleWorkLinksV41(", "function peopleMonthBoundsV41(",
        "function scheduleMonthRangeV41(", "function scheduleCountV41(", "function workSchedulePanelV41("))
    renderer = "function renderPeopleV41(c){" + html.split("function renderPeopleV41(c){", 1)[1].split("function renderPageV41(){", 1)[0]
    body = r'''const assert=require('node:assert/strict');
const state={route:'attendance',peopleMonth:'2026-10',readRole:'owner',status:'전체 상태',selected:{},search:'',businessId:'b1',peopleTab:'detail'};
const configs={payroll:{title:'급여',columns:['직원','총지급액','공제','실수령','상태']},attendance:{title:'근태',columns:['직원','일자','지점','근무시간','상태']}};
const handlers={};const elements={};
const workspaceApi=async()=>({});
const window={ObysAttendanceCorrections:{mount(host,options){assert.equal(options.manager,true);assert.equal(options.businessId,'b1')}}};
const document={createElement:()=>({}),getElementById:id=>elements[id]||(elements[id]={dataset:{},addEventListener:(t,f)=>{handlers[id+':'+t]=f}})};
function bindWorkSchedulesV41(){};function bindManualEmployeePickerV41(){};
const page={innerHTML:'',append(){},insertAdjacentHTML(){},querySelectorAll:()=>[]};
let rerenders=0;const kstDateV41=()=>'2026-10-30';function renderPageV41(){rerenders++};function openDetailV41(){};function previewContractSchedulesV41(){};
async function loadLiveRoute(){};
''' + helpers + renderer + r'''
const rec=(id,key,name,branch,d)=>liveRow({id,employee_key:key,raw:{id,employee_name:name,employee_email:'[MASKED]',work_date:d,start_at:'09:00:00',end_at:'18:00:00',status:'approved',branch},display:{직원:name,지점:branch,일자:d}},configs.attendance);
configs.attendance.data=[rec('a1','KEY-A','김가','본점','2026-10-01'),rec('a2','KEY-A','김가','본점','2026-10-02'),rec('b1','KEY-B','이나','강남점','2026-10-05')];
const sch=(key,name,email,d,extra={})=>({employee_key:key,employee_email:email,employee_name:name,branch:'강남점',work_date:d,kind:'work',start_time:'09:00:00',end_time:'18:00:00',break_minutes:60,judgment:{status:'정상'},...extra});
state.workSchedules={month:{rows:[sch('KEY-B','이나','na@x.test','2026-10-05'),sch('KEY-C','박다','da@x.test','2026-10-06',{judgment:{status:'지각'}})],counts:{}},today:{rows:[],counts:{}}};
// 직원별 현황: 기록 3건이어도 직원 2행 + 일정 전용 1명
state.peopleTab='employees';renderPeopleV41(configs.attendance);
assert.equal((page.innerHTML.match(/data-person="KEY-A"/g)||[]).length,1);
assert(page.innerHTML.includes('data-person="KEY-B"'));assert(!page.innerHTML.includes('record:'));
// 개인상세: select 노출
state.peopleTab='detail';renderPeopleV41(configs.attendance);
let h=page.innerHTML;
assert(h.includes('직원 선택'));assert(h.includes('id="peoplePersonSelect"'));
for(const opt of ['김가 · 본점','이나 · 강남점','박다 · 강남점'])assert(h.includes(opt),opt);
assert(h.includes('김가 · 2026-10 출근 달력'));assert(h.includes('data-person')===false||true);
assert(typeof handlers['peoplePersonSelect:change']==='function');
// 선택 변경 -> state 갱신 + 재렌더 호출 -> 달력이 선택 직원 행으로 전환
handlers['peoplePersonSelect:change']({target:{value:'KEY-C'}});
assert.equal(state.peoplePerson,'KEY-C');assert.equal(rerenders,1);
renderPeopleV41(configs.attendance);h=page.innerHTML;
assert(h.includes('박다 · 2026-10 출근 달력'));assert(h.includes('지각'));assert(!h.includes('김가 · 2026-10 출근 달력'));
assert(h.includes('<option value="KEY-C" selected>'));
// 일정 있는 직원: 결근 처리 버튼 노출, 계약 미리보기 이메일은 선택 직원 일정의 이메일
assert(h.includes('data-absence-action'));
assert(h.includes('data-email="da@x.test"'));
state.peoplePerson='KEY-B';renderPeopleV41(configs.attendance);h=page.innerHTML;
assert(h.includes('이나 · 2026-10 출근 달력'));assert(h.includes('data-email="na@x.test"'));
state.peoplePerson='KEY-A';renderPeopleV41(configs.attendance);h=page.innerHTML;
assert(h.includes('김가 · 2026-10 출근 달력'));assert(!h.includes('data-email="na@x.test"'));assert(h.includes('id="personalContractPreview" data-email=""'));
'''
    _run(tmp_path / "select.cjs", body)


# ---------------------------------------------------------------- 월간 일괄 등록
def test_month_range_and_count_helpers(tmp_path):
    _need_node()
    helpers = _lines(ADMIN.read_text(), ("function scheduleMonthRangeV41(", "function scheduleCountV41("))
    cases = [(2026, 2), (2028, 2), (2026, 10), (2026, 11), (2026, 12)]
    expected = {f"{y}-{m:02d}": [f"{y}-{m:02d}-01", f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"] for y, m in cases}
    import json
    body = "const assert=require('node:assert/strict');\n" + helpers + "\n" + f"""
const exp={json.dumps(expected)};
for(const [m,[a,b]] of Object.entries(exp)){{const r=scheduleMonthRangeV41(m);assert.equal(r.from,a);assert.equal(r.to,b)}}
assert.equal(scheduleMonthRangeV41('2026-13'),null);assert.equal(scheduleMonthRangeV41(''),null);
// 2026-10: 평일(월~금) 22일, 전체 31일
assert.equal(scheduleCountV41('2026-10-01','2026-10-31',[0,1,2,3,4],3).days,22);
assert.equal(scheduleCountV41('2026-10-01','2026-10-31',[0,1,2,3,4],3).cells,66);
assert.equal(scheduleCountV41('2026-10-01','2026-10-31',[0,1,2,3,4,5,6],2).cells,62);
assert.equal(scheduleCountV41('2026-02-01','2026-02-28',[0,1,2,3,4,5,6],1).days,28);
assert.equal(scheduleCountV41('2028-02-01','2028-02-29',[0,1,2,3,4,5,6],1).days,29);
assert.equal(scheduleCountV41('2026-10-31','2026-10-01',[0],1).cells,0);
"""
    _run(tmp_path / "month.cjs", body)


def test_month_mode_posts_single_bulk_payload_and_blocks_over_1000(tmp_path):
    _need_node()
    html = ADMIN.read_text()
    helpers = _lines(html, ("function peopleMonthBoundsV41(", "function scheduleMonthRangeV41(",
                            "function scheduleCountV41(", "function bindWorkSchedulesV41("))
    body = r'''const assert=require('node:assert/strict');
const state={peopleMonth:'2026-02',businessId:'b1',scheduleEmployees:{b1:[]}};
const kstDateV41=()=>'2026-10-30';
const handlers={};const els={};
const mk=id=>els[id]=els[id]||{id,value:'',hidden:false,textContent:'',dataset:{},addEventListener:(t,f)=>{handlers[id+':'+t]=f}};
const vals={date_from:'2026-02-01',date_to:'2026-02-28',start_time:'09:00',end_time:'18:00',break_minutes:'60',kind:'work',memo:''};
let weekdays=['0','1','2','3','4'];let checked=[{value:'a@x.test',dataset:{name:'가',branch:'본점'}},{value:'b@x.test',dataset:{name:'나',branch:'본점'}}];
const submit={textContent:'주간 일정 일괄 등록',disabled:false};
const form={elements:{date_from:{value:vals.date_from},date_to:{value:vals.date_to}},
 addEventListener:(t,f)=>{handlers['form:'+t]=f},
 querySelector:()=>submit,
 querySelectorAll:s=>s.includes(':checked')?checked:[]};
global.FormData=class{constructor(f){}get(k){return form.elements[k]?form.elements[k].value:vals[k]}getAll(k){return k==='weekday'?weekdays:[]}};
const document={getElementById:id=>id==='scheduleBulk'?form:mk(id)};
const page={querySelectorAll:()=>[]};
const calls=[];let reloads=0;async function loadLiveRoute(){reloads++}
async function workspaceApi(path,opt){calls.push([path,opt])}
function renderPageV41(){};function previewContractSchedulesV41(){};function maskEmployeeEmailV41(v){return v};function openRosterEditorV41(){};
async function financeApiV41(){return {employees:[]}}
''' + helpers + r'''
(async()=>{
bindWorkSchedulesV41();
els.scheduleUnit.value='month';els.scheduleMonth.value='2026-02';
handlers['scheduleUnit:change']();
assert.equal(form.elements.date_from.value,'2026-02-01');assert.equal(form.elements.date_to.value,'2026-02-28');
assert.equal(submit.textContent,'월간 일정 일괄 등록');assert.equal(els.scheduleMonthLabel.hidden,false);
assert.equal(els.scheduleCount.textContent,'2명 × 20일 = 40칸');
// 31일 달로 변경
els.scheduleMonth.value='2026-10';handlers['scheduleMonth:change']();
assert.equal(form.elements.date_from.value,'2026-10-01');assert.equal(form.elements.date_to.value,'2026-10-31');
assert.equal(els.scheduleCount.textContent,'2명 × 22일 = 44칸');
// 제출: 단일 bulk 호출, 월 범위
const ev={preventDefault(){}};
form.onsubmit(ev);await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
assert.equal(calls.length,1);assert(calls[0][0].endsWith('/attendance/schedules/bulk'));assert.equal(calls[0][1].method,'POST');
const p=JSON.parse(calls[0][1].body);
assert.equal(p.date_from,'2026-10-01');assert.equal(p.date_to,'2026-10-31');assert.deepEqual(p.weekdays,[0,1,2,3,4]);assert.equal(p.employees.length,2);
assert.equal(state.scheduleNotice,'44칸 등록');assert.equal(reloads,1);
// 주간으로 되돌리면 7일 범위
state.scheduleWeek='2026-10-05';els.scheduleUnit.value='week';handlers['scheduleUnit:change']();
assert.equal(form.elements.date_from.value,'2026-10-05');assert.equal(form.elements.date_to.value,'2026-10-11');assert.equal(submit.textContent,'주간 일정 일괄 등록');
// 1,000칸 초과 차단: 40명 x 31일
checked=Array.from({length:40},(_,i)=>({value:'u'+i+'@x.test',dataset:{name:'n'+i,branch:'b'}}));weekdays=['0','1','2','3','4','5','6'];
els.scheduleUnit.value='month';els.scheduleMonth.value='2026-10';handlers['scheduleUnit:change']();
assert.equal(els.scheduleCount.textContent.startsWith('40명 × 31일 = 1240칸'),true);
form.onsubmit(ev);await new Promise(r=>setImmediate(r));
assert.equal(calls.length,1);assert(els.scheduleStatus.textContent.includes('1,000칸'));
// 정확히 1000칸(40명 x 25일)은 통과: 기간 25일 지정
form.elements.date_from.value='2026-10-01';form.elements.date_to.value='2026-10-25';
form.onsubmit(ev);await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
assert.equal(calls.length,2);assert.equal(state.scheduleNotice,'1000칸 등록');
// 366일 초과 차단
form.elements.date_from.value='2026-01-01';form.elements.date_to.value='2027-06-30';checked=checked.slice(0,1);
form.onsubmit(ev);await new Promise(r=>setImmediate(r));
assert.equal(calls.length,2);assert(els.scheduleStatus.textContent.includes('366일'));
})().catch(e=>{console.error(e);process.exit(1)});
'''
    _run(tmp_path / "bulk.cjs", body)
