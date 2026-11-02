"""Execute calendar period transitions and month-boundary reads from admin.html."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_roster_period_navigation_and_boundary_reads():
    html = (ROOT / "app/static/apps/obys/admin.html").read_text()
    names = ["displayDateV41", "scheduleMonthRangeV41", "captureScheduleDraftV41", "restoreScheduleDraftV41",
             "scheduleNavigateV41", "scheduleShiftV41", "loadWorkSchedulesV41", "workSchedulePanelV41"]
    source = "\n".join(next(line.strip() for line in html.splitlines()
                            if line.strip().startswith((f"function {name}(", f"async function {name}(")))
                       for name in names)
    script = r'''
const assert = require('node:assert/strict');
const state = {businessId:'b',peopleMonth:'2026-11',scheduleWeek:'2026-10-26',scheduleView:'week',readRole:'admin',
  scheduleEmployees:{b:[{employee_email:'one@example.test',employee_name:'직원'}]},
  scheduleDraft:{businessId:'b',values:{start_time:'22:00',end_time:'06:00',break_minutes:'0'},employees:['one@example.test'],weekdays:[0,2]}};
const document = {getElementById:()=>null}, configs = {attendance:{data:[]}};
const peopleMonthBoundsV41 = () => ({month:state.peopleMonth,...scheduleMonthRangeV41(state.peopleMonth)});
const maskEmployeeEmailV41 = value => value;
const kstDateV41 = () => '2026-11-02';
let reloads = 0, paths=[];
const loadLiveRoute = async () => {reloads++;};
const workspaceApi = async path => {paths.push(new URL('http://fixture'+path));return {schedules:[],counts:{}};};
''' + source + r'''
(async()=>{
 await scheduleNavigateV41('month','2026-11');
 assert.equal(state.scheduleUnit,'month');assert.equal(state.scheduleView,'month');
 assert.equal(state.scheduleDraft.values.date_from,'2026-11-01');
 assert.equal(state.scheduleDraft.values.date_to,'2026-11-30');
 assert.equal(state.scheduleDraft.values.break_minutes,'0');
 assert.deepEqual(state.scheduleDraft.employees,['one@example.test']);
 assert.deepEqual(state.scheduleDraft.weekdays,[0,2]);
 assert.equal(state.scheduleDraft.values.start_time,'22:00');
 await scheduleNavigateV41('week','2026-12-28');await scheduleShiftV41(7);
 assert.equal(state.peopleMonth,'2027-01');assert.equal(state.scheduleWeek,'2027-01-04');
 assert.equal(state.scheduleDraft.values.date_to,'2027-01-10');
 await scheduleShiftV41(-7);assert.equal(state.scheduleWeek,'2026-12-28');
 await scheduleNavigateV41('month','2028-02');assert.equal(state.scheduleDraft.values.date_to,'2028-02-29');
 const before=reloads;await scheduleNavigateV41('month','invalid');assert.equal(reloads,before);
 state.peopleMonth='2026-11';state.scheduleWeek='2026-10-26';state.scheduleView='week';
 await loadWorkSchedulesV41('b',peopleMonthBoundsV41());
 assert.equal(paths.length,3);
 assert.ok(paths.some(path=>path.searchParams.get('from')==='2026-10-26'&&path.searchParams.get('to')==='2026-11-01'));
 paths=[];state.scheduleView='month';await loadWorkSchedulesV41('b',peopleMonthBoundsV41());
 assert.equal(paths.length,2);
 state.workSchedules={month:{rows:[{employee_email:'one@example.test',work_date:'2026-11-30',kind:'work',start_time:'22:00',end_time:'06:00'}]},roster:{rows:[]}};
 const esc=value=>String(value??'');const monthHTML=workSchedulePanelV41(esc);
 assert.equal((monthHTML.split('</thead>')[0].match(/<th>/g)||[]).length,31);
 assert.ok(monthHTML.includes('22:00–06:00'));
 assert.ok(monthHTML.includes('2026-11-30 (월)'));
 assert.ok(monthHTML.includes('2026-11-01 (일) ~ 2026-11-30 (월)'));
 assert.ok(!monthHTML.includes('2026-11-31'));
 assert.equal(displayDateV41('2028-02-29'),'2028-02-29 (화)');
 assert.equal(displayDateV41('2026-02-29'),'확인필요');
 assert.equal(displayDateV41(''),'미등록');
 assert.equal(displayDateV41('bad'),'확인필요');
 assert.equal(displayDateV41('2026-12-31T15:30:00Z'),'2027-01-01 (금)');
 state.scheduleView='week';const weekHTML=workSchedulePanelV41(esc);
 assert.ok(weekHTML.includes('2026-10-26 (월) ~ 2026-11-01 (일)'));
 assert.equal((weekHTML.split('</thead>')[0].match(/<th>/g)||[]).length,8);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    import os
    for tz in ["UTC", "Asia/Seoul", "America/Los_Angeles", "Pacific/Kiritimati"]:
        result = subprocess.run(["node", "-e", script], env={**os.environ, "TZ": tz}, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, tz + result.stdout + result.stderr
