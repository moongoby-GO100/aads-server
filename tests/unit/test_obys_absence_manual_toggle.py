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
    # 정책 변경(재제출 5): 결근·복원 판정은 서버가 한다. 화면은 이메일·날짜·동작만 /attendance/absence 로 보낸다.
    assert "/attendance/absence" in html and "employee_email:email,work_date:day,action:act" in html
    assert "data-absence-action" in html and "min-height:44px" in html
    assert 'section("근무일정 · 출근/결근 수동 처리 — 달력에서도 처리할 수 있습니다"' in html


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
                "function displayDateV41(", "function attendanceTimeV41(", "function attendanceDisplayV41(", "function liveRow(", "function filteredRows(c)",
                "function peopleWorkLinksV41(", "function peopleMonthBoundsV41(", "function employmentMatchV41(",
                "function workSchedulePanelV41(")
    helpers = "\n".join(next(line.strip() for line in html.splitlines() if line.strip().startswith(p)) for p in prefixes)
    renderer = "function renderPeopleV41(c){" + html.split("function renderPeopleV41(c){", 1)[1].split("function renderPageV41(){", 1)[0]
    script = tmp_path / "absence.cjs"
    script.write_text('''const assert=require('node:assert/strict');
const state={route:'attendance',peopleMonth:'2026-10',readRole:'owner',status:'전체 상태',selected:{},search:'',businessId:'b1',peopleTab:'detail',peoplePerson:'k@x.test'};
const configs={payroll:{title:'급여',columns:['직원','총지급액','공제','실수령','상태']},attendance:{title:'근태',columns:['직원','일자','지점','근무시간','상태']}};
const mounts=[],hosts=[];
const window={ObysAttendanceCorrections:{mount(host,options){mounts.push({host,options});assert.equal(options.manager,true);assert.equal(options.businessId,'b1');assert.equal(options.api,workspaceApi);assert.equal(host,hosts.at(-1));assert.equal(host.className,'section')}}};
const elements={};const document={createElement:tag=>{assert.equal(tag,'section');return {}},getElementById:id=>elements[id]||(elements[id]={addEventListener(){}})};
function bindWorkSchedulesV41(){};function bindManualEmployeePickerV41(){};
let buttons=[];const page={innerHTML:'',append(host){hosts.push(host)},insertAdjacentHTML(){},querySelectorAll:s=>s==='[data-absence-action]'?buttons:[]};
const kstDateV41=()=>'2026-10-30';function renderPageV41(){};function openDetailV41(){};
const calls=[],toasts=[];let reloads=0;async function loadLiveRoute(){reloads++}
async function workspaceApi(path,opt){calls.push([path,opt]);}
function showToast(m){toasts.push(m)};let confirms=[];global.confirm=m=>{confirms.push(m);return true};
''' + helpers + renderer + '''
const sch=(d,extra={})=>({employee_key:'k@x.test',employee_email:'k@x.test',employee_name:'김직원',work_date:d,kind:'work',start_time:'09:00:00',end_time:'18:00:00',break_minutes:60,judgment:{status:'정상'},...extra});
state.workSchedules={month:{rows:[sch('2026-10-01'),sch('2026-10-02',{judgment:{status:'결근'}}),sch('2026-10-03',{judgment:{status:'결근'}}),sch('2026-10-31'),sch('2026-10-05',{kind:'off'}),sch('2026-10-06',{judgment:{status:'출근(기본)',labels:['출근(기본)'],default_present:true},default_present:true,action_record:null}),sch('2026-10-07',{judgment:{status:'결근',labels:['결근']},action_record:{id:'m1',status:'rejected',memo:'결근(대표 확인)',start_at:'09:00:00',end_at:'18:00:00',break_minutes:60,hourly_wage:0,employee_name:'김직원'}})],counts:{'출근(기본)':1}},today:{rows:[sch('2026-10-30',{judgment:{status:'출근(기본)',labels:['출근(기본)'],default_present:true},default_present:true,action_record:null})],counts:{'출근':1}}};
const rec=(id,d,status)=>liveRow({id,employee_key:'k@x.test',raw:{id,employee_name:'김직원',employee_email:'k@x.test',work_date:d,start_at:'09:00:00',end_at:'18:00:00',break_minutes:60,hourly_wage:10000,status,branch:'본점'},display:{직원:'김직원',일자:d}},configs.attendance);
configs.attendance.data=[rec('r1','2026-10-01','approved'),rec('r2','2026-10-02','rejected')];
renderPeopleV41(configs.attendance);
assert.equal(mounts.length,1);assert.equal(hosts.length,1);
const html=page.innerHTML;
assert(html.includes('2026-10-01 (목)'));
assert(html.includes('aria-label="2026-10-30 (금)" aria-current="date"'));
assert.equal((html.match(/data-day=/g)||[]).length,31);
assert(html.includes('<span></span>'.repeat(4)));
assert(!html.includes('data-day="2026-11-01"'));
assert.equal(attendanceTimeV41('2026-12-31T15:30:00Z'),'2027-01-01 (금) 00:30');
assert.equal(attendanceTimeV41('2027-01-01T00:30:00'),'2027-01-01 (금) 00:30');
assert(html.includes('결근으로 변경'));assert(html.includes('출근으로 되돌리기'));assert(html.includes('출근으로 처리'));
assert(html.includes('위치기반 출퇴근이 안정화될 때까지'));
assert(!html.includes('data-absence-day="2026-10-31"'));assert(!html.includes('data-absence-day="2026-10-05"'));
assert(/data-absence-action="mark-absent"[^>]*data-absence-day="2026-10-06"/.test(html));
assert(/data-absence-action="restore-present"[^>]*data-absence-day="2026-10-07"[^>]*data-employee-email="k@x.test"/.test(html));
assert(!html.includes('data-record-id'));
assert(html.includes('data-business="b1"'));
const mk=(action,day)=>({dataset:{absenceAction:action,absenceDay:day,employeeEmail:'k@x.test',business:'b1'},disabled:false});
buttons=[mk('mark-absent','2026-10-01'),mk('restore-present','2026-10-02'),mk('mark-present','2026-10-03')];
renderPeopleV41(configs.attendance);
(async()=>{
 assert.equal(mounts.length,2);assert.equal(hosts.length,2);
 for(const b of buttons)await b.onclick();
 assert.equal(calls.length,3);assert.equal(reloads,3);assert.equal(toasts.length,0);
 assert(confirms[0].includes('2026-10-01')&&confirms[0].includes('김직원'));
 const body=i=>JSON.parse(calls[i][1].body);
 // 화면은 기록 ID 를 판정하지 않는다 — 세 동작 모두 서버 한 엔드포인트로 (이메일, 날짜, 동작)만 보낸다
 for(let i=0;i<3;i++){assert.equal(calls[i][1].method,'POST');assert(calls[i][0].endsWith('/attendance/absence'),calls[i][0]);assert.deepEqual(Object.keys(body(i)).sort(),['action','employee_email','work_date']);assert.equal(body(i).employee_email,'k@x.test')}
 assert.deepEqual([0,1,2].map(i=>body(i).action),['mark-absent','restore-present','mark-present']);
 assert.deepEqual([0,1,2].map(i=>body(i).work_date),['2026-10-01','2026-10-02','2026-10-03']);
 calls.length=0;reloads=0;
 const dm=(action,day,extra={})=>({dataset:{absenceAction:action,absenceDay:day,employeeEmail:'k@x.test',business:'b1',...extra},disabled:false});
 // 그린 시점과 다른 사업자면 API 를 부르지 않는다
 calls.length=0;const stale=dm('mark-absent','2026-10-06',{business:'other'});buttons=[stale];renderPeopleV41(configs.attendance);await stale.onclick();
 assert.equal(calls.length,0);assert(toasts.at(-1).includes('사업자가 바뀌어'));
 // 오늘 현황판 · 직원별 현황 에서도 같은 버튼이 보인다
 state.peopleTab='today';renderPeopleV41(configs.attendance);
 assert(/data-absence-action="mark-absent"[^>]*data-absence-day="2026-10-30"/.test(page.innerHTML));
 state.peopleTab='employees';renderPeopleV41(configs.attendance);
 assert(/data-absence-action="restore-present"[^>]*data-absence-day="2026-10-07"/.test(page.innerHTML));
 state.peopleTab='detail';toasts.length=0;
 const m0=mounts.length,h0=hosts.length;state.readRole='member';renderPeopleV41(configs.attendance);assert(!page.innerHTML.includes('결근으로 변경'));assert.equal(mounts.length,m0);assert.equal(hosts.length,h0);
})().catch(e=>{console.error(e);process.exit(1)});
''')
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _schedule():
    return {"kind": "work", "work_date": "2026-10-10", "start_time": "09:00", "end_time": "18:00", "break_minutes": 60}


def _record(status):
    return {"start_at": "09:00", "end_at": "18:00", "break_minutes": 60, "status": status}


NOW = datetime(2026, 10, 30, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))


def test_explicit_admin_absence_on_past_work_day_is_absent():
    assert api.attendance_schedule_judgment(_schedule(), {**_record("rejected"), "memo": "결근(대표 확인)"}, now=NOW)["status"] == "결근"
    assert api.attendance_schedule_judgment({**_schedule(), "actual_records": [{**_record("rejected"), "memo": "결근(대표 확인)"}]}, now=NOW)["status"] == "결근"


def test_approved_record_on_past_work_day_is_normal():
    assert api.attendance_schedule_judgment(_schedule(), _record("approved"), now=NOW)["status"] == "정상"


@pytest.mark.parametrize("memo", [None, "", "일반 반려", "결근", "결근(대표 확인) 추가"])
def test_ordinary_rejection_remains_unconfirmed(memo):
    record = {**_record("rejected"), "memo": memo}
    assert api.attendance_schedule_judgment(_schedule(), record, now=NOW)["status"] == "확인필요"


def test_restoring_manual_absence_and_mixed_records():
    absence = {**_record("rejected"), "memo": "결근(대표 확인)"}
    restored = {**absence, "status": "approved", "memo": "출근(수기 확인)"}
    assert api.attendance_schedule_judgment(_schedule(), restored, now=NOW)["status"] == "정상"
    # 복원은 같은 기록을 갱신하므로 결근 기록이 남은 채 정상 기록이 함께 있으면 수동 결근이 그날 전체에 적용된다
    # (급여 초안이 absent_days 로 날짜 전체를 빼는 것과 같은 규칙 — 리뷰 지적 1).
    for records in ([absence, restored], [restored, absence]):
        assert api.attendance_schedule_judgment(_schedule(), records, now=NOW)["status"] == "결근"


@pytest.mark.parametrize("day,status", [("2026-10-10", "출근(기본)"), ("2026-10-30", "출근(기본)"), ("2026-10-31", "예정")])
def test_no_record_does_not_automatically_become_absent(day, status):
    assert api.attendance_schedule_judgment({**_schedule(), "work_date": day}, now=NOW)["status"] == status


@pytest.mark.asyncio
async def test_schedule_query_keeps_explicit_absence():
    from datetime import date
    from unittest.mock import AsyncMock

    record = {**_record("rejected"), "memo": "결근(대표 확인)",
              "employee_email": "staff@example.com", "work_date": date(2026, 10, 10)}
    connection = AsyncMock()
    connection.fetch.return_value = [record]
    grouped = await api._schedule_actual_records(connection, "b1", [record])
    sql = connection.fetch.call_args.args[0]
    # 수동 결근뿐 아니라 일반 반려도 판정 입력에 남긴다(runner-be4aa6f8 지적 P2) — 상태 필터 없음
    assert "a.status <> 'rejected'" not in sql
    assert "a.business_id=$1 AND a.deleted_at IS NULL" in sql
    assert api.attendance_schedule_judgment(_schedule(), next(iter(grouped.values())), now=NOW)["status"] == "결근"


@pytest.mark.asyncio
async def test_schedule_list_counts_manual_absence_without_check_in(monkeypatch):
    from unittest.mock import AsyncMock

    schedule = {**_schedule(), "employee_email": "staff@example.com"}
    absence = {**schedule, **_record("rejected"), "memo": "결근(대표 확인)"}
    connection = AsyncMock()
    connection.fetch.side_effect = [[schedule], [absence]]
    monkeypatch.setattr(api, "_schedule_business", AsyncMock(return_value={"name": "테스트 사업자"}))
    monkeypatch.setattr(api, "_require_route_read", lambda *args: None)
    monkeypatch.setattr(api.upload_svc, "_tenant", lambda user: "tenant")
    monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=connection))
    monkeypatch.setattr(api, "_legacy_people_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(api.finance_svc, "separated_last_work_dates", lambda *a: {})
    result = await api.list_work_schedules("b1", date_from="2026-10-10", date_to="2026-10-10",
                                           employee_email=None, offset=0, current_user={})
    assert result["schedules"][0]["judgment"]["status"] == "결근"
    assert result["schedules"][0]["has_check_in"] is False
    assert result["counts"] == {"결근": 1}


def test_calendar_cell_inline_action_static(tmp_path):
    """출근 달력 칸에서 바로 처리: 칸에 처리 버튼 템플릿, 중첩 button 없음, 핸들러 1곳."""
    html = ADMIN.read_text()
    assert 'absenceBtnV41(day," calendar-day-act")' in html
    assert '<div class="calendar-day"><button class="btn calendar-day-open" data-day=' in html
    assert '<button class="btn calendar-day"' not in html
    assert len(re.findall(r'querySelectorAll\("\[data-absence-action\]"\)', html)) == 1
    assert '<details class="absence-cards">' in html
    # 달력 칸 템플릿 안에 button 이 중첩되지 않는다: 칸 시작~끝 사이 열린 button 은 항상 닫힌 뒤 다음 button
    start = html.index('<div class="calendar-day"><button')
    cell = html[start:html.index("}).join(", start)] if False else html[start:html.index("`}).join(\"\")}</div>`)", start)]
    depth = 0
    for tok in re.findall(r"</?button\b", cell):
        depth += -1 if tok.startswith("</") else 1
        assert 0 <= depth <= 1
