"""Execute the existing admin UI functions against the server read contract."""
import ast
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / 'app/static/apps/obys/admin.html').read_text()


def line(prefix):
    return next(s.strip() for s in HTML.splitlines() if s.strip().startswith(prefix))


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_role_navigation_and_payroll_execute():
    tree = ast.parse((ROOT / 'app/api/obys_workspaces.py').read_text())
    roles = next(n for n in tree.body if isinstance(n, ast.AnnAssign)
                 and getattr(n.target, 'id', '') == 'ROUTE_READ_ROLES')
    restricted = [ast.literal_eval(k) for k in roles.value.keys]
    menu = re.search(r'const menu=(\[.*?\n    \]);', HTML, re.S).group(1)
    source = '\n'.join(line(p) for p in [
        'const MANAGER_READ_ROUTES=', 'function canReadRouteV41(',
        'function peopleWorkLinksV41(', 'function renderNav(', 'function payrollMoneyV41(',
        'function payrollDisplayV41(', 'function liveRow(',
        'async function loadLiveRoute(',
    ])
    code = '''const assert=require('node:assert/strict');
const state={}, nav={innerHTML:''}, configs={home:{columns:[]},payroll:{columns:['급여번호','직원','총지급액','기본급','수당','공제','실수령','상태']}};
''' + f'const menu={menu};\n' + source + f'''
assert.deepEqual([...MANAGER_READ_ROUTES].sort(), {json.dumps(restricted)}.sort());
for(const role of [null,'unknown','member','viewer','owner','admin']){{
 state.readRole=role;renderNav();
 for(const route of MANAGER_READ_ROUTES){{
  assert.equal(nav.innerHTML.includes(`data-route="${{route}}"`),['owner','admin'].includes(role),`${{role}} ${{route}}`);
 }}
 assert.equal(nav.innerHTML.includes('data-route="attendance"'),['owner','admin','member'].includes(role));
 assert.ok(nav.innerHTML.includes('data-route="home"'));
}}
const raw={{gross_pay:'3000000',tax_withholding:'100000',insurance_deduction:200000,other_deduction:'0',net_pay:'2700000'}};
const row=liveRow({{id:'p1',status:'확정',raw,display:{{직원:'테스트',기본급:'잘못된 값'}}}},configs.payroll);
assert.equal(row.총지급액,'3,000,000원');assert.equal(row.공제,'미집계');assert.equal(row.실수령,'2,700,000원');
assert.equal(row.기본급,'미집계');assert.equal(row.수당,'미집계');
assert.equal(payrollDisplayV41({{raw:{{...raw,other_deduction:null}}}}).공제,'미집계');
assert.equal(payrollDisplayV41({{raw:{{gross_pay:0,net_pay:0}}}}).총지급액,'0원');
for(const value of [null,undefined,'',' ',true,[], 'NaN','Infinity'])assert.equal(payrollMoneyV41(value),'미집계');
assert.equal(payrollDisplayV41({{}}).실수령,'미집계');
assert.equal(payrollDisplayV41({{raw:{{total_deductions:300000}}}}).공제,'300,000원');
const other={{columns:['직원']}};assert.equal(liveRow({{display:{{직원:'유지'}}}},other).직원,'유지');
const paths=[];const history={{replaceState(){{}}}},document={{getElementById:()=>null}};
function renderPageV41(){{}};function renderSessionRecovery(){{throw Error('unexpected session failure')}};
async function workspaceApi(path){{paths.push(path);return {{records:[],metrics:[]}}}}
Object.assign(state,{{readRole:'member',route:'payroll',businessId:'b',requestId:0}});
loadLiveRoute().then(()=>{{assert.equal(state.route,'home');assert.equal(paths.length,2);assert(paths.every(p=>p.includes('/home/')))}}).catch(e=>{{console.error(e);process.exitCode=1}});

'''
    subprocess.run(['node', '-e', code], check=True, capture_output=True, text=True)


def test_permission_gates_run_before_requests_and_render():
    load = line('async function loadLiveRoute(')
    assert load.index('canReadRouteV41(state.route)') < load.index('workspaceApi(')
    render = HTML.split('function renderPageV41(){', 1)[1]
    assert render.index('canReadRouteV41(state.route)') < render.index('filteredRows(c)')
    start = HTML.split('async function startWorkspaceV41(){', 1)[1].split('startWorkspaceV41().catch', 1)[0]
    # 읽기 권한 확인이 사업자·직원 조회와 화면 데이터 로드보다 먼저 끝나야 한다.
    assert start.index('await loadReadPermissionV41();') < start.index('await Promise.all(')
    assert start.index('await loadReadPermissionV41();') < start.index('await loadLiveRoute();')


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_membership_source_and_permission_retry_execute():
    source = '\n'.join(line(p) for p in [
        'const MANAGER_READ_ROUTES=', 'function canReadRouteV41(',
        'function renderNav(', 'async function loadReadPermissionV41(',
        'function handleStartupError(',
        'function renderSessionRecovery(',
    ])
    source += '\nasync function startWorkspaceV41(){' + HTML.split('async function startWorkspaceV41(){', 1)[1].split('    const initial=', 1)[0]
    menu = re.search(r'const menu=(\[.*?\n    \]);', HTML, re.S).group(1)
    code = '''const assert=require('node:assert/strict');
const state={route:'attendance'},nav={innerHTML:''},page={innerHTML:''};
const elements={businessName:{},businessMeta:{},loginRecoveryBtn:{},recoveryRetryBtn:{}};
const document={getElementById:id=>elements[id]};
function escapeHtml(s){return s};let logins=0;function clearAuthAndOpenLogin(){logins++};
function peopleWorkLinksV41(){return ""};async function loadEmployeePreviewPermissionV41(){};
let payload, failure, renders=0, organizations=0, businesses=0, loads=0;
async function organizationApi(path){assert.equal(path,'/me');if(failure)throw failure;return payload}
function renderPageV41(){renders++};async function loadOrganizations(){organizations++};
async function loadBusinesses(){businesses++};async function loadLiveRoute(){loads++};
''' + f'const menu={menu};\n' + source + '''
(async()=>{
 for(const role of ['member','owner','admin','viewer']){
  // Legacy finance role is employee; it must never determine workspace access.
  payload={tenant_id:'t',membership:{tenant_id:'t',status:'active',role},permissions:{role:'employee'}};
  await loadReadPermissionV41();assert.equal(state.readRole,role);
  assert.equal(canReadRouteV41('attendance'),role!=='viewer');
  assert.equal(canReadRouteV41('payroll'),['owner','admin'].includes(role));
 }
 for(const status of [503,401]){
  state.route='attendance';failure=Object.assign(new Error('temporary failure'),{status});
  const before=loads;
  await startWorkspaceV41().catch(handleStartupError);
  assert.equal(state.readRole,null);assert.equal(loads,before);
  assert.equal(state.route,'attendance');if(status===401)assert.equal(logins,1);else assert.ok(page.innerHTML.includes('recoveryRetryBtn'));
  assert.equal(nav.innerHTML.includes('data-route="payroll"'),false);
  failure=null;payload={tenant_id:'t',membership:{tenant_id:'t',status:'active',role:'member'}};
  await elements.recoveryRetryBtn.onclick();
  assert.equal(state.readRole,'member');assert.equal(loads,before+1);
  assert.equal(state.route,'attendance');
  assert.ok(nav.innerHTML.includes('data-route="attendance"'));
 }
 for(const membership of [{tenant_id:'other',status:'active',role:'owner'},
     {tenant_id:'t',status:'suspended',role:'owner'},{tenant_id:'t',status:'active',role:'employee'},{}]){
  payload={tenant_id:'t',membership};await assert.rejects(loadReadPermissionV41());
  assert.equal(state.readRole,null);
 }
 assert.equal(organizations,2);assert.equal(businesses,2);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    subprocess.run(['node', '-e', code], check=True, capture_output=True, text=True)


def test_auth_me_exposes_the_workspace_membership_source():
    auth = (ROOT / 'app/api/auth.py').read_text()
    me = auth.split('async def get_me(', 1)[1]
    assert '"membership": current_user.get("current_membership")' in me
    service = (ROOT / 'app/services/obys_upload_service.py').read_text()
    session = service.split('def tenant_session_for_user(', 1)[1].split('\ndef ', 1)[0]
    assert 'membership = user.get("current_membership") or {}' in session
    assert 'membership_tenant != tenant_id' in session
    assert 'status != "active"' in session
    permission = line('async function loadReadPermissionV41(')
    assert 'organizationApi("/me")' in permission
    assert 'financeApiV41' not in permission
    assert 'if(!state.readRole)' in line('async function loadLiveRoute(')
