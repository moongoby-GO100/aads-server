"""같은 날 [정상 기록 + 수동 결근]: 세 화면 모두 이메일·날짜·동작만 서버로 보낸다(기록 ID·동료 기록 판정 없음) — 실제 처리기를 Node 로 실행."""
import shutil
import subprocess
from pathlib import Path

import pytest

ADMIN = Path("app/static/apps/obys/admin.html")


def test_mixed_day_three_views_point_to_manual_absence_record(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node 없음")
    html = ADMIN.read_text()
    prefixes = ("function escapeHtml(", "function metricHtml(", "function payrollMoneyV41(",
                "function payrollDisplayV41(", "function attendanceStatusV41(",
                "function attendanceReviewV41(", "function attendanceMinutesV41(",
                "function displayDateV41(", "function attendanceTimeV41(", "function attendanceDisplayV41(", "function liveRow(", "function filteredRows(c)",
                "function peopleWorkLinksV41(", "function peopleMonthBoundsV41(", "function employmentMatchV41(",
                "function workSchedulePanelV41(")
    helpers = "\n".join(next(line.strip() for line in html.splitlines() if line.strip().startswith(p)) for p in prefixes)
    renderer = "function renderPeopleV41(c){" + html.split("function renderPeopleV41(c){", 1)[1].split("function renderPageV41(){", 1)[0]
    script = tmp_path / "mixed.cjs"
    script.write_text('''const assert=require('node:assert/strict');
const state={route:'attendance',peopleMonth:'2026-10',readRole:'owner',status:'전체 상태',selected:{},search:'',businessId:'b1',peopleTab:'detail',peoplePerson:'k@x.test'};
const configs={attendance:{title:'근태',columns:['직원','일자','지점','근무시간','상태']}};
const window={ObysAttendanceCorrections:{mount(){}}};
const elements={};const document={createElement:()=>({}),getElementById:id=>elements[id]||(elements[id]={addEventListener(){}})};
function bindWorkSchedulesV41(){};function bindManualEmployeePickerV41(){};
let buttons=[];const page={innerHTML:'',append(){},insertAdjacentHTML(){},querySelectorAll:s=>s==='[data-absence-action]'?buttons:[]};
const kstDateV41=()=>'2026-10-30';function renderPageV41(){};function openDetailV41(){};
const calls=[],toasts=[];let reloads=0;async function loadLiveRoute(){reloads++}
async function workspaceApi(path,opt){calls.push([path,opt]);}
function showToast(m){toasts.push(m)};global.confirm=()=>true;
''' + helpers + renderer + '''
const absent={id:'m1',status:'rejected',memo:'결근(대표 확인)',start_at:'09:00:00',end_at:'18:00:00',break_minutes:60,hourly_wage:10000,employee_name:'김직원'};
const row={employee_key:'k@x.test',employee_email:'k@x.test',employee_name:'김직원',work_date:'2026-10-30',kind:'work',start_time:'09:00:00',end_time:'18:00:00',break_minutes:60,judgment:{status:'결근',labels:['결근']},action_record:absent};
state.workSchedules={month:{rows:[row],counts:{'결근':1}},today:{rows:[row],counts:{'결근':1}}};
const rec=(id,status,email)=>liveRow({id,employee_key:email,raw:{id,employee_name:'김직원',employee_email:email,work_date:'2026-10-30',start_at:'09:00:00',end_at:'18:00:00',break_minutes:60,hourly_wage:10000,status,memo:status==='rejected'?'결근(대표 확인)':'',branch:'본점'},display:{직원:'김직원',일자:'2026-10-30'}},configs.attendance);
// A(k@x.test)는 정상 기록 + 수동 결근(같은 날). 화면은 기록 ID 를 고르지 않고 이메일·날짜·동작만 서버로 보낸다.
configs.attendance.data=[rec('r1','approved','k@x.test'),rec('m1','rejected','k@x.test')];
const btn=h=>[...h.matchAll(/<button[^>]*data-absence-action="([^"]+)"[^>]*data-absence-day="2026-10-30"[^>]*data-employee-email="([^"]*)"[^>]*>([^<]*)<\\/button>/g)].map(m=>({act:m[1],email:m[2],lab:m[3]}));
(async()=>{
 for(const tab of ['detail','today','employees']){
  state.peopleTab=tab;buttons=[];renderPeopleV41(configs.attendance);
  const found=btn(page.innerHTML);
  assert(found.length>=1,tab+' 버튼 없음');
  for(const f of found){assert.equal(f.act,'restore-present',tab);assert.equal(f.email,'k@x.test',tab);assert.equal(f.lab,'출근으로 되돌리기',tab)}
  assert(!page.innerHTML.includes('data-record-id'),tab+' 에 record-id 가 남음');
 }
 // [테스트 1] selected=A 인 화면에서 B 의 버튼 클릭 → 본문 employee_email=B, /attendance/records 호출 0건
 state.peopleTab='today';state.peoplePerson='k@x.test';
 const b={dataset:{absenceAction:'restore-present',absenceDay:'2026-10-30',employeeEmail:'other@x.test',business:'b1'},disabled:false};
 buttons=[b];renderPeopleV41(configs.attendance);
 await b.onclick();
 assert.equal(calls.length,1);assert.equal(reloads,1);assert.equal(toasts.length,0);
 assert(calls[0][0].endsWith('/attendance/absence'),calls[0][0]);assert.equal(calls[0][1].method,'POST');
 const body=JSON.parse(calls[0][1].body);
 assert.deepEqual(body,{employee_email:'other@x.test',work_date:'2026-10-30',action:'restore-present'});
 assert.equal(calls.filter(c=>c[0].includes('/attendance/records')).length,0);
 assert.equal(calls.filter(c=>c[1].method==='PUT'||c[1].method==='DELETE').length,0);
 // 이메일을 모르는 버튼은 서버를 부르지 않는다
 calls.length=0;const nb={dataset:{absenceAction:'mark-absent',absenceDay:'2026-10-30',employeeEmail:'',business:'b1'},disabled:false};
 buttons=[nb];renderPeopleV41(configs.attendance);await nb.onclick();assert.equal(calls.length,0);
})().catch(e=>{console.error(e);process.exit(1)});
''')
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
