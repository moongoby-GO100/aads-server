"""관리자 근태 표시: 실제 목록 계약과 화면 함수 실행 검증."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / 'app/static/apps/obys/admin.html').read_text()


def line(prefix):
    return next(s.strip() for s in reversed(HTML.splitlines()) if s.strip().startswith(prefix))


def test_attendance_source_and_empty_state():
    config = line('attendance:C(')
    assert 'source:src(' in config and 'source:none(' not in config
    assert '"records","obys.yeoljeong_attendance_records"' in config
    assert '등록된 근태 기록이 없습니다.' in HTML
    assert '["근태 건수|0건","누락·수정 대기|0건"]' in HTML
    api = (ROOT / 'app/api/obys_workspaces.py').read_text()
    assert 'ATTENDANCE_STATUSES = frozenset({"pending", "approved", "rejected"})' in api
    columns = api.split('ATTENDANCE_COLUMNS = (', 1)[1].split(')', 1)[0]
    assert 'memo' in columns and 'geofence_result' not in columns
    migration = (ROOT / 'migrations/20260930_obys_attendance_pwa_gps.sql').read_text()
    assert 'ADD COLUMN IF NOT EXISTS geofence_result TEXT' in migration
    display = line('function attendanceDisplayV41(')
    assert 'raw.memo' in display and 'geofence' not in display
    assert 'attendanceDisplayV41(record)' in line('function liveRow(')


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_attendance_mapping_and_api_execute():
    source = '\n'.join(line(prefix) for prefix in [
        'function attendanceStatusV41(', 'function attendanceReviewV41(',
        'function attendanceMinutesV41(', 'function attendanceDisplayV41(',
        'async function attendanceLoadV41(', 'function liveRow(',
        'async function loadLiveRoute(', 'function filteredRows(',
    ])
    code = '''const assert=require('node:assert/strict');
const state={readRole:'admin',route:'attendance',businessId:'b',dateFrom:'2026-10-01',dateTo:'2026-10-23',requestId:0,status:'전체 상태',selected:{},tab:'records'};
const configs={attendance:{columns:['근태번호','일자','지점','직원','근무시간','이상유형','상태'],hierarchy:[]}};
const document={getElementById:()=>null};function canReadRouteV41(){return true};function renderPageV41(){};
let paths=[], payload={records:[],source:{name:'yeoljeong_attendance_records',live:true}};
async function workspaceApi(path){paths.push(path);return payload};
''' + source + '''
(async()=>{
 for(const [raw,expected] of [
  [{worked_minutes:125},'2시간 5분'],[{worked_minutes:0},'0시간 0분'],
  [{start_at:'22:00:00',end_at:'02:30:00',break_minutes:30},'4시간 0분'],
  [{worked_minutes:null,start_at:'09:00',end_at:'18:00',break_minutes:60},'8시간 0분'],
  [{},'미산출'],[{start_at:'09:00'},'미산출'],
  [{start_at:'09:00',end_at:'09:00'},'미산출'],
  [{start_at:'09:00',end_at:'10:00',break_minutes:90},'미산출']]){
  assert.equal(attendanceDisplayV41({raw}).근무시간,expected);
 }
 for(const [status,expected] of [['pending','확인필요'],['approved','승인'],['rejected','반려'],['unknown','unknown']]){
  const row=liveRow({id:'a',raw:{status,memo:'GPS 확인 요청'},display:{일자:'2026-10-23',직원:'직원',지점:'지점'}},configs.attendance);
  assert.equal(row.상태,expected);assert.equal(row.이상유형,'GPS 확인 요청');assert.equal(row.직원,'직원');
 }
 assert.equal(attendanceDisplayV41({raw:{geofence_result:'outside',status:'pending'}}).이상유형,'—');
 payload.records=['pending','approved','rejected','확인 필요','확인필요'].map(status=>({id:status,status,raw:{status}}));
 await loadLiveRoute();assert.equal(paths.length,1);
 const url=new URL(paths[0],'http://test');assert.equal(url.pathname,'/b/attendance/records');
 assert.equal(url.searchParams.get('from'),'2026-10-01');assert.equal(url.searchParams.get('to'),'2026-10-23');
 assert.equal(url.searchParams.has('date_from'),false);
 assert.deepEqual(state.liveMetrics,[{label:'근태 건수',value:'5건'},{label:'누락·수정 대기',value:'3건'}]);
 assert.equal(state.liveSource.live,true);assert.equal(state.liveHasMore,false);
 state.status='확인필요';assert.equal(filteredRows(configs.attendance).length,2);
 state.status='전체 상태';state.tab='exceptions';assert.equal(filteredRows(configs.attendance).length,3);
 payload.records=[];await loadLiveRoute();assert.equal(state.liveMetrics[0].value,'0건');assert.equal(state.liveSource.live,true);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
