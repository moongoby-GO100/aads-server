"""대표가 직원 달력에서 결근으로 변경·출근으로 되돌릴 수 있는지 검증한다 (위치기반 출퇴근 안정화 전 수기 병행)."""
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.api import obys_workspaces as api

ADMIN = Path("app/static/apps/obys/admin.html")


def test_absence_labels_and_status_wiring_are_static():
    html = ADMIN.read_text()
    for label in ("결근으로 변경", "출근으로 되돌리기", "출근으로 처리"):
        assert label in html
    assert "위치기반 출퇴근이 안정화될 때까지 결근은 수기로 확인·수정합니다." in html
    assert 'status=act==="mark-absent"?"rejected":"approved"' in html
    assert '"결근(대표 확인)"' in html and '"출근(수기 확인)"' in html
    assert 'method:rec?"PUT":"POST"' in html
    assert "data-absence-action" in html and "min-height:44px" in html
    assert 'section("근무일정 · 출근/결근 수동 처리"' in html


def test_browser_script_syntax(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node 없음")
    script = tmp_path / "admin.js"
    script.write_text("\n".join(re.findall(r"<script>(.*?)</script>", ADMIN.read_text(), re.S)))
    subprocess.run([node, "--check", str(script)], check=True, capture_output=True)


def test_absence_buttons_render_and_call_api(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node 없음")
    html = ADMIN.read_text()
    prefixes = ("function escapeHtml(", "function metricHtml(", "function payrollMoneyV41(",
                "function payrollDisplayV41(", "function attendanceStatusV41(",
                "function attendanceReviewV41(", "function attendanceMinutesV41(",
                "function attendanceDisplayV41(", "function liveRow(", "function filteredRows(c)",
                "function peopleWorkLinksV41(", "function peopleMonthBoundsV41(",
                "function workSchedulePanelV41(")
    helpers = "\n".join(next(line.strip() for line in html.splitlines() if line.strip().startswith(p)) for p in prefixes)
    renderer = "function renderPeopleV41(c){" + html.split("function renderPeopleV41(c){", 1)[1].split("function renderPageV41(){", 1)[0]
    script = tmp_path / "absence.cjs"
    script.write_text('''const assert=require('node:assert/strict');
const state={route:'attendance',peopleMonth:'2026-10',readRole:'owner',status:'전체 상태',selected:{},search:'',businessId:'b1',peopleTab:'detail',peoplePerson:'k@x.test'};
const configs={payroll:{title:'급여',columns:['직원','총지급액','공제','실수령','상태']},attendance:{title:'근태',columns:['직원','일자','지점','근무시간','상태']}};
const elements={};const document={getElementById:id=>elements[id]||(elements[id]={addEventListener(){}})};
function bindWorkSchedulesV41(){};function bindManualEmployeePickerV41(){};
let buttons=[];const page={innerHTML:'',insertAdjacentHTML(){},querySelectorAll:s=>s==='[data-absence-action]'?buttons:[]};
const kstDateV41=()=>'2026-10-30';function renderPageV41(){};function openDetailV41(){};
const calls=[],toasts=[];let reloads=0;async function loadLiveRoute(){reloads++}
async function workspaceApi(path,opt){calls.push([path,opt]);}
function showToast(m){toasts.push(m)};let confirms=[];global.confirm=m=>{confirms.push(m);return true};
''' + helpers + renderer + '''
const sch=(d,extra={})=>({employee_key:'k@x.test',employee_email:'k@x.test',employee_name:'김직원',work_date:d,kind:'work',start_time:'09:00:00',end_time:'18:00:00',break_minutes:60,judgment:{status:'정상'},...extra});
state.workSchedules={month:{rows:[sch('2026-10-01'),sch('2026-10-02',{judgment:{status:'결근'}}),sch('2026-10-03',{judgment:{status:'결근'}}),sch('2026-10-31'),sch('2026-10-05',{kind:'off'})],counts:{}},today:{rows:[],counts:{}}};
const rec=(id,d,status)=>liveRow({id,employee_key:'k@x.test',raw:{id,employee_name:'김직원',employee_email:'k@x.test',work_date:d,start_at:'09:00:00',end_at:'18:00:00',break_minutes:60,hourly_wage:10000,status,branch:'본점'},display:{직원:'김직원',일자:d}},configs.attendance);
configs.attendance.data=[rec('r1','2026-10-01','approved'),rec('r2','2026-10-02','rejected')];
renderPeopleV41(configs.attendance);
const html=page.innerHTML;
assert(html.includes('결근으로 변경'));assert(html.includes('출근으로 되돌리기'));assert(html.includes('출근으로 처리'));
assert(html.includes('위치기반 출퇴근이 안정화될 때까지'));
assert(!html.includes('data-absence-day="2026-10-31"'));assert(!html.includes('data-absence-day="2026-10-05"'));
const mk=(action,day,id)=>({dataset:{absenceAction:action,absenceDay:day,recordId:id},disabled:false});
buttons=[mk('mark-absent','2026-10-01','r1'),mk('restore-present','2026-10-02','r2'),mk('mark-present','2026-10-03','')];
renderPeopleV41(configs.attendance);
(async()=>{
 for(const b of buttons)await b.onclick();
 assert.equal(calls.length,3);assert.equal(reloads,3);assert.equal(toasts.length,0);
 assert(confirms[0].includes('2026-10-01')&&confirms[0].includes('김직원'));
 const body=i=>JSON.parse(calls[i][1].body);
 assert.equal(calls[0][1].method,'PUT');assert(calls[0][0].endsWith('/attendance/records/r1'));
 assert.equal(body(0).status,'rejected');assert.equal(body(0).memo,'결근(대표 확인)');assert.equal(body(0).start_at,'09:00');assert.equal(body(0).hourly_wage,10000);
 assert.equal(calls[1][1].method,'PUT');assert.equal(body(1).status,'approved');
 assert.equal(calls[2][1].method,'POST');assert(calls[2][0].endsWith('/attendance/records'));
 assert.equal(body(2).status,'approved');assert.equal(body(2).memo,'출근(수기 확인)');assert.equal(body(2).end_at,'18:00');assert.equal(body(2).break_minutes,60);assert.equal(body(2).employee_email,'k@x.test');
 state.readRole='member';renderPeopleV41(configs.attendance);assert(!page.innerHTML.includes('결근으로 변경'));
})().catch(e=>{console.error(e);process.exit(1)});
''')
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _schedule():
    return {"kind": "work", "work_date": "2026-10-10", "start_time": "09:00", "end_time": "18:00", "break_minutes": 60}


def _record(status):
    return {"start_at": "09:00", "end_at": "18:00", "break_minutes": 60, "status": status}


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def test_rejected_record_on_past_work_day_is_absent():
    assert api.attendance_schedule_judgment(_schedule(), _record("rejected"), now=NOW)["status"] == "결근"
    assert api.attendance_schedule_judgment({**_schedule(), "actual_records": [_record("rejected")]}, now=NOW)["status"] == "결근"


def test_approved_record_on_past_work_day_is_normal():
    assert api.attendance_schedule_judgment(_schedule(), _record("approved"), now=NOW)["status"] == "정상"
