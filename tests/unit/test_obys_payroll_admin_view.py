"""오비서 관리자 급여 현황 표시 결함 — 최신 월 선택·건수 선택월 기준·상세 자동 펼침 제거. node 로 실제 실행."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[2] / "app/static/apps/obys/admin.html"
NODE = shutil.which("node") or "/usr/bin/node"
pytestmark = pytest.mark.skipif(not Path(NODE).exists(), reason="node 없음")


def _fn(src: str, name: str) -> str:
    m = re.search(r"^    function " + name + r"\(.*$", src, re.M)
    assert m, name
    return m.group(0)


def _run(body: str):
    src = HTML.read_text(encoding="utf-8")
    fns = "\n".join(_fn(src, n) for n in ("payrollLatestMonthV41", "payrollMonthRowsV41", "payrollDetailRowV41"))
    out = subprocess.run([NODE, "-e", fns + "\n" + body], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


ROWS = ("const rows=[['a','2025-10','1'],['b','2026-09','2'],['c','2026-09','3'],['d','2026-08','4']]"
        ".map(([id,m,k])=>({__live:{id,raw:{payroll_month:m}},k}));const ident=r=>r.__live.id;")


def test_latest_month_picked_from_records():
    assert _run(ROWS + "console.log(JSON.stringify(payrollLatestMonthV41(rows)))") == "2026-09"


def test_no_records_returns_empty_so_caller_uses_current_month():
    assert _run("console.log(JSON.stringify(payrollLatestMonthV41([])))") == ""


def test_user_selected_month_not_overridden_in_source():
    src = HTML.read_text(encoding="utf-8")
    assert "payrollLatestMonthV41(c.data)||kstDateV41().slice(0,7)" in src
    assert 'if(state.route==="payroll"){state.payrollMonth=e.target.value;state.payrollMonthSelected=true}else{state.peopleMonth=e.target.value;state.peopleMonthSelected=true}' in src


def test_count_is_selected_month_only():
    assert _run(ROWS + "console.log(JSON.stringify(payrollMonthRowsV41(rows,'2026-09').length))") == 2
    assert _run(ROWS + "console.log(JSON.stringify(payrollMonthRowsV41(rows,'2026-11').length))") == 0


def test_header_count_uses_month_rows():
    src = HTML.read_text(encoding="utf-8")
    assert "<span>${payroll?inMonth.length:data.length}건</span>" in src
    assert "const inMonth=payroll?payrollMonthRowsV41(data,month)" in src


def test_detail_not_auto_expanded():
    assert _run(ROWS + "console.log(JSON.stringify(payrollDetailRowV41(rows,'',ident)))") is None
    assert _run(ROWS + "console.log(JSON.stringify(payrollDetailRowV41(rows,undefined,ident)))") is None
    assert _run(ROWS + "console.log(JSON.stringify(payrollDetailRowV41(rows,'b',ident).k))") == "2"
    assert "if(selected){const row=inMonth.find" not in HTML.read_text(encoding="utf-8")


def _run_load(scenario: str):
    """실제 activeMonthV41·peopleMonthBoundsV41·peopleLoadV41·loadLiveRoute 를 node 로 실행한다(의존 객체만 대역)."""
    src = HTML.read_text(encoding="utf-8")
    fns = "\n".join(
        re.search(r"^    (?:async )?function " + n + r"\(.*$", src, re.M).group(0)
        for n in ("payrollLatestMonthV41", "activeMonthV41", "peopleMonthBoundsV41", "peopleLoadV41", "loadLiveRoute")
    )
    prelude = """
const calls=[],schedMonths=[];let latestRows=[];
const state={peopleMonth:'2026-11',payrollMonth:'',payrollMonthSelected:false,route:'attendance',businessId:'b1',readRole:'manager',requestId:0,dateFrom:'',dateTo:'',search:'',status:'전체 상태',recordOffset:0,selected:{}};
const configs={attendance:{data:[]},payroll:{data:[]}};
const kstDateV41=()=>'2026-11-05';
const loadWorkSchedulesV41=async(b,bounds)=>{schedMonths.push(bounds.month);return null};
const canReadRouteV41=()=>true;
const renderPageV41=()=>{};
const liveRow=r=>({__live:{raw:r}});
const history={replaceState(){}};
const document={getElementById:()=>null};
const handleStartupError=e=>{throw e};
const clearAuthAndOpenLogin=()=>{};
const workspaceApi=async(path)=>{calls.push(path);const q=new URLSearchParams(path.split('?')[1]);const m=q.get('date_from');
  const recs=path.includes('/payroll/')?latestRows.filter(r=>!m||r.payroll_month===m.slice(0,7)):[];return {records:recs,has_more:false,next_offset:0}};
const apiMonth=i=>{const q=new URLSearchParams(calls[i].split('?')[1]);return (q.get('date_from')||q.get('from')||'').slice(0,7)};
"""
    body = prelude + fns + "\n(async()=>{" + scenario + "})().catch(e=>{console.error(e);process.exit(1)})"
    out = subprocess.run([NODE, "-e", body], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_ROWS_JS = "latestRows=[{payroll_month:'2026-09'},{payroll_month:'2026-08'},{payroll_month:'2025-10'}];"


def test_scenario1_attendance_08_then_payroll_first_entry_is_latest_09():
    r = _run_load(_ROWS_JS + """
state.peopleMonth='2026-08';state.peopleMonthSelected=true;
state.route='payroll';await loadLiveRoute();
console.log(JSON.stringify({pay:state.payrollMonth,att:state.peopleMonth,filtered:calls[0].includes('date_from'),n:configs.payroll.data.length}))""")
    assert r == {"pay": "2026-09", "att": "2026-08", "filtered": False, "n": 3}


def test_scenario2_payroll_auto_09_then_attendance_back_is_08_in_screen_api_and_schedule():
    r = _run_load(_ROWS_JS + """
state.peopleMonth='2026-08';state.peopleMonthSelected=true;
state.route='payroll';await loadLiveRoute();
state.route='attendance';await loadLiveRoute();
console.log(JSON.stringify({att:state.peopleMonth,api:apiMonth(calls.length-1),sched:schedMonths}))""")
    assert r == {"att": "2026-08", "api": "2026-08", "sched": ["2026-08"]}


def test_scenario3_payroll_manual_08_then_attendance_10_then_payroll_back_is_08_in_api():
    r = _run_load(_ROWS_JS + """
state.route='payroll';await loadLiveRoute();
state.payrollMonth='2026-08';state.payrollMonthSelected=true;await loadLiveRoute();
state.route='attendance';state.peopleMonth='2026-10';state.peopleMonthSelected=true;await loadLiveRoute();
state.route='payroll';await loadLiveRoute();
console.log(JSON.stringify({pay:state.payrollMonth,att:state.peopleMonth,api:apiMonth(calls.length-1),n:configs.payroll.data.length}))""")
    assert r == {"pay": "2026-08", "att": "2026-10", "api": "2026-08", "n": 1}


def test_scenario4_no_records_falls_back_to_current_month():
    r = _run_load("state.route='payroll';await loadLiveRoute();console.log(JSON.stringify({month:state.payrollMonth}))")
    assert r == {"month": "2026-11"}


def test_scenario5_count_follows_selected_month_and_store_filter_in_source():
    src = HTML.read_text(encoding="utf-8")
    assert "<span>${payroll?inMonth.length:data.length}건</span>" in src
    assert "(!state.payrollMonth||state.route!==\"payroll\"||r.__live?.raw?.payroll_month===state.payrollMonth)" in src
    assert "peopleMonthBeforePayroll" not in src and "payrollMonthAuto" not in src


def test_scenario6_no_auto_expand_detail_in_source():
    src = HTML.read_text(encoding="utf-8")
    assert "if(selected){const row=inMonth.find" not in src
    assert "payrollDetailRowV41(inMonth,state.payrollDetailKey,identity)" in src
