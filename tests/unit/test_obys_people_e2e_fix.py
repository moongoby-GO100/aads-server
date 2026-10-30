"""운영 데이터 호출 없이 초기 접근 가드와 메뉴 계약을 실행 검증한다."""
import ast
import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
INDEX = (ROOT / 'app/static/apps/obys/index.html').read_text()
ADMIN = (ROOT / 'app/static/apps/obys/admin.html').read_text()


@pytest.mark.parametrize('legacy', [False, True])
def test_session_field_comes_from_server_tenant_decision(legacy):
    module = ast.parse((ROOT / 'app/api/obys_finance.py').read_text())
    func = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'get_session')
    func.decorator_list = []
    func.args.defaults = []
    func.args.args[0].annotation = None
    func.returns = None
    calls = []

    async def tenant(user):
        calls.append('tenant')
        return {'permissions': {'role': 'owner'}, 'user': user}

    async def thread(fn, user):
        calls.append('legacy')
        return fn(user)

    scope = {'is_legacy_obys_tenant': lambda user: legacy, 'upload_svc': SimpleNamespace(session_for_current_user=tenant),
             'svc': SimpleNamespace(session_for_user=lambda user: {'permissions': {'role': 'owner'}, 'user': user}), 'run_in_threadpool': thread}
    exec(compile(ast.Module(body=[func], type_ignores=[]), '<session>', 'exec'), scope)
    result = asyncio.run(scope['get_session']({'tenant_id': 'test'}))
    assert result['legacy_access'] is legacy
    assert calls == ['legacy' if legacy else 'tenant']
    assert result['permissions']['role'] == 'owner'


@pytest.mark.skipif(shutil.which('node') is None, reason='node 미설치 런타임')
def test_guard_and_menu_execute_before_legacy_requests(tmp_path):
    guard = INDEX.split('    async function verifyLegacyEntry() {')[1].split('    async function init() {')[0]
    menu = ADMIN.split('    function peopleWorkLinksV41(){')[1].split('    function renderNav()')[0]
    script = tmp_path / 'guard.cjs'
    script.write_text('''const assert=require('node:assert/strict');
const state={settings:{businesses:[{id:'stale-other-tenant'}],branches:[{name:'stale'}]}};
let authed=true, legacy=false, fail=false, businesses=[], calls=[], saves=0;
const document={body:{innerHTML:''},getElementById:()=>({textContent:''})};
const hasServerAuth=()=>authed;const saveAuthSession=()=>{saves++};
async function financeApi(path){calls.push(path);if(fail)throw Error('멀티테넌트 격리 작업이 진행 중입니다.');return path==='/session'?{legacy_access:legacy}:{businesses}}
async function verifyLegacyEntry(){''' + guard + '''
function peopleWorkLinksV41(){''' + menu + '''
(async()=>{
 for (const scenario of ['nonlegacy','unlinked','failure','legacy']){
  legacy=scenario!=='nonlegacy';fail=scenario==='failure';businesses=scenario==='unlinked'?[]:[{id:'allowed',name:'테스트',branches:[{name:'테스트 지점'}]}];calls=[];saves=0;
  state.settings={businesses:[{id:'settings-store'}],branches:[{name:'settings-branch'}]};
  const ok=await verifyLegacyEntry();assert.equal(ok,(scenario==='legacy'||scenario==='unlinked'));assert.equal(saves,ok?1:0);
  if(ok){assert.deepEqual(state.settings.businesses,[{id:'settings-store'}]);assert.deepEqual(state.settings.branches,[{name:'settings-branch'}]);}
  assert(calls.length<=2);assert(!calls.some(x=>/payroll|sales|settings/.test(x)));
  if(!ok){assert.deepEqual(state.settings.businesses,[]);assert.deepEqual(state.settings.branches,[]);assert(!/멀티테넌트|열정국밥|중화점|biz-junghwa/.test(document.body.innerHTML));assert.equal((document.body.innerHTML.match(/<a /g)||[]).length,1)}
 }
 state.legacyAccess=false;assert(!peopleWorkLinksV41().includes('legacy=1'));assert(peopleWorkLinksV41().includes('출퇴근 하기'));
 state.legacyAccess=true;assert.equal((peopleWorkLinksV41().match(/legacy=1/g)||[]).length,1);
})().catch(e=>{console.error(e);process.exitCode=1});''')
    result = subprocess.run(['node', str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'if (!await verifyLegacyEntry()) return;' in INDEX.split('async function init() {')[1].split('initSelects();')[0]
    assert 'window.OBYS_DEVELOPMENT_TOOLS === true' in INDEX
    assert 'permissions.can_manage_settings === true' in INDEX


@pytest.mark.skipif(shutil.which('node') is None, reason='node 미설치 런타임')
def test_production_header_and_unlinked_empty_state(tmp_path):
    policy = INDEX.split('    function syncDevelopmentTools() {')[1].split('    function setView(view) {')[0]
    empty = ADMIN.split('if(state.businessesLoaded&&!state.businessId){')[1].split('      const selected=c.hierarchy.map')[0]
    script = tmp_path / 'presentation.cjs'
    script.write_text("""const assert=require('node:assert/strict');
let hidden, authenticated=true;const sessionPermissions={can_manage_settings:true},window={};
const hasServerAuth=()=>authenticated,authPermissions=()=>sessionPermissions;
const document={querySelectorAll:()=>[{classList:{toggle:(_name,value)=>hidden=value}}]};
function policy(){""" + policy + """
policy();assert.equal(hidden,true);
window.OBYS_DEVELOPMENT_TOOLS=true;policy();assert.equal(hidden,false);
sessionPermissions.can_manage_settings=false;policy();assert.equal(hidden,true);
sessionPermissions.can_manage_settings=true;authenticated=false;policy();assert.equal(hidden,true);
const state={businessesLoaded:true,businessId:''},c={title:'급여 현황'},page={innerHTML:''};
const escapeHtml=x=>x;
function render(){if(state.businessesLoaded&&!state.businessId){""" + empty + """}
render();assert(page.innerHTML.includes('연결된 사업자가 없습니다'));
assert(page.innerHTML.includes('사업자 등록하기'));assert(!page.innerHTML.includes('DB 연결 확인 필요'));
""")
    result = subprocess.run(['node', str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    for name in ['clobe-collection', 'data-overview']:
        assert f'data-legacy-module src="/static/apps/obys/modules/{name}.js' in INDEX
    for name in ['sampleBtn', 'importBtn', 'exportBtn', 'resetBtn']:
        assert f'id="{name}" data-development-tool' in INDEX


@pytest.mark.skipif(shutil.which('node') is None, reason='node 미설치 런타임')
def test_legacy_modules_load_once_in_order_and_report_source(tmp_path):
    loader = INDEX.split('    let legacyEntryModulesPromise;')[1].split('    // Check once')[0]
    script = tmp_path / 'modules.cjs'
    script.write_text('''const assert=require('node:assert/strict');
let appended=[], sources=[{src:'first.js'},{src:'second.js'}], fail=false;
const document={readyState:'complete',querySelectorAll:()=>sources,
createElement:()=>({dataset:{}}),head:{appendChild(s){
assert.equal(s.async,false);appended.push(s.src);
queueMicrotask(()=>fail?s.onerror():s.onload());}}};
let legacyEntryModulesPromise;''' + loader + '''
(async()=>{
 await Promise.all([loadLegacyEntryModules(),loadLegacyEntryModules()]);
 assert.deepEqual(appended,['first.js','second.js']);
 legacyEntryModulesPromise=undefined;sources=[];
 await assert.rejects(loadLegacyEntryModules(),/tags missing/);
 sources=[{src:'first.js'},{src:'second.js'}];legacyEntryModulesPromise=undefined;fail=true;
 await assert.rejects(loadLegacyEntryModules(),/first.js/);
 assert.deepEqual(appended,['first.js','second.js','first.js']);
})().catch(e=>{console.error(e);process.exitCode=1});''')
    result = subprocess.run(['node', str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'console.error("Legacy entry initialization failed", error)' in INDEX
    guard = INDEX.split('async function verifyLegacyEntry()')[1].split('async function init()')[0]
    assert '/tenant-registry/businesses' not in guard
    assert 'state.settings.businesses = businesses' not in guard


def test_development_tools_toggle_has_its_own_auth_scope():
    """두 호출처가 다른 함수의 지역 변수를 참조하는 회귀를 차단한다."""
    helper = INDEX.split('    function syncDevelopmentTools() {')[1].split('    function setView(view) {')[0]
    assert 'const isAuthed = hasServerAuth();' in helper
    assert 'const permissions = authPermissions();' in helper
    toggles = [line for line in INDEX.splitlines() if 'querySelectorAll("[data-development-tool]")' in line]
    assert len(toggles) == 1
    assert toggles[0] in helper
    for name, end in [('setView(view)', 'function '), ('renderAuthState()', 'function ')]:
        body = INDEX.split('    function ' + name + ' {')[1].split('    ' + end)[0]
        assert 'syncDevelopmentTools();' in body
        assert 'permissions.can_manage_settings' not in body


@pytest.mark.skipif(shutil.which('node') is None, reason='node 미설치 런타임')
def test_development_tools_execute_without_caller_local_variables(tmp_path):
    helper = INDEX.split('    function syncDevelopmentTools() {')[1].split('    function setView(view) {')[0]
    script = tmp_path / 'development-tools.cjs'
    script.write_text('''const assert=require('node:assert/strict');
let authenticated=false, currentPermissions={}, hidden;
const window={};
const hasServerAuth=()=>authenticated, authPermissions=()=>currentPermissions;
const document={querySelectorAll:selector=>{
 assert.equal(selector,'[data-development-tool]');
 return [{classList:{toggle:(name,value)=>{assert.equal(name,'hidden');hidden=value}}}];
}};
function syncDevelopmentTools(){''' + helper + '''
for (const auth of [false,true]) {
 for (const permission of [undefined,false,true]) {
  for (const enabled of [undefined,false,true]) {
   authenticated=auth;currentPermissions={can_manage_settings:permission};
   window.OBYS_DEVELOPMENT_TOOLS=enabled;
   syncDevelopmentTools();
   assert.equal(hidden,!(auth && permission===true && enabled===true));
  }
 }
}
''')
    result = subprocess.run(['node', str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def js_function(source, name):
    """함수 끝은 줄바꿈 대신 Node의 JavaScript 파서로 확인한다."""
    result = subprocess.run(['node', '-e', r"""
const source=require('node:fs').readFileSync(0,'utf8');
const name=process.argv[1];
const matches=[...source.matchAll(new RegExp('(?:async\\s+)?function\\s+'+name+'\\s*\\(', 'g'))];
const start=matches.at(-1)?.index??-1;
if(start<0)throw Error('missing function '+name);
for(let end=source.indexOf('}',start);end>=0;end=source.indexOf('}',end+1)){
 const candidate=source.slice(start,end+1);
 try{new Function('return ('+candidate+'\n)');}catch{continue;}
 require('node:fs').writeFileSync(1,candidate);process.exit(0);
}
throw Error('incomplete function '+name);
""", name], input=source, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(shutil.which('node') is None, reason='node 미설치 런타임')
def test_admin_waits_for_session_and_business_before_mounting_people(tmp_path):
    functions = '\n'.join(js_function(ADMIN, name) for name in
                          ['startWorkspaceV41', 'renderPageV41', 'loadBusinesses', 'handleStartupError'])
    script = tmp_path / 'startup.cjs'
    script.write_text('''const assert=require('node:assert/strict');
let state, calls, releaseSession, failBusiness=false, recovery;
const page={innerHTML:''},elements={};
const document={getElementById:id=>elements[id]??={},querySelectorAll:()=>[],querySelector:()=>null};
const localStorage={getItem:()=>null,setItem:()=>{}};
const escapeHtml=x=>x,configs={home:{title:'홈',hierarchy:[],data:[]}};
const canReadRouteV41=()=>true,routeInfo=()=>({group:{label:'업무'}}),filteredRows=()=>[];
async function loadReadPermissionV41(){state.readRole='owner';calls.push('permission');}
async function loadEmployeePreviewPermissionV41(){calls.push('session');await new Promise(r=>releaseSession=r);}
async function loadOrganizations(){calls.push('organizations');}
async function workspaceApi(){calls.push('businesses');if(failBusiness)throw Error('business failed');return {businesses:[]};}
async function loadLiveRoute(){assert(state.workspaceReady);calls.push('route');renderPageV41();}
function renderSessionRecovery(message){recovery=message;page.innerHTML='다시 시도';}
function clearAuthAndOpenLogin(){throw Error('unexpected login');}
''' + functions + '''
(async()=>{
 for(const failure of [false,true]){
  state={route:'home',businessId:'',businessesLoaded:false};calls=[];recovery='';failBusiness=failure;
  const pending=startWorkspaceV41().catch(handleStartupError);
  await new Promise(r=>setImmediate(r));
  assert(calls.includes('businesses'),'slow session must not delay business lookup');
  assert(!calls.includes('route'));assert(!state.workspaceReady);
  if(!failure){assert(state.businessesLoaded);assert(page.innerHTML.includes('확인하고 있습니다'));}
  releaseSession();await pending;
  if(failure){assert.equal(recovery,'business failed');assert(page.innerHTML.includes('다시 시도'));}
  else{assert(calls.includes('route'));assert(page.innerHTML.includes('사업자 등록하기'));}
 }
})().catch(e=>{console.error(e);process.exitCode=1});''')
    result = subprocess.run(['node', str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_legacy_entry_saves_session_only_after_legacy_access_is_confirmed():
    """비레거시 세션 저장에 따른 대시보드 자동 호출을 차단한다."""
    guard = INDEX.split('    async function verifyLegacyEntry() {')[1].split('    async function init() {')[0]
    before, allowed = guard.split('if (session.legacy_access === true) {', 1)
    assert 'saveAuthSession(' not in before
    legacy_block, denied = allowed.split('return true;', 1)
    assert 'saveAuthSession({...session, permissions_verified: true});' in legacy_block
    assert 'saveAuthSession(' not in denied
