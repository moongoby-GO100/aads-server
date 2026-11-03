"""직원 진입과 식별 격리 회귀: 실제 함수를 Node에서 실행한다."""
import subprocess
from pathlib import Path

from tests.unit.test_obys_employee_preview_menu import _function_source

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / 'app/static/apps/obys/index.html').read_text()


def run_js(source):
    result = subprocess.run(['node', '-e', source], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_verified_assignment_identity_and_company_store_isolation():
    source = _function_source(HTML, 'isCurrentEmployeeRecord')
    run_js('''const assert=require('node:assert/strict');
    let employeePreview=null, authSession={user:{id:'u1',tenant_id:'t1'}};
    let employeeJoinRequests=[{id:'e1',email:'one@example.test',status:'approved',business_id:'b1',branch:'A'}];
    let email='one@example.test', requestId='e1';
    const isEmployeeExperience=()=>true, currentEmployeeEmail=()=>email;
    const authPermissions=()=>({employee_request_id:requestId});
    ''' + source + '''
    const row={employee_email:email,employee_request_id:'e1',business_id:'b1',branch:'A',tenant_id:'t1'};
    assert.equal(isCurrentEmployeeRecord(row),true);
    for(const patch of [{business_id:'b2'},{branch:'B'},{tenant_id:'t2'}, {employee_request_id:'e2'}, {employee_email:'other@example.test'}, {user_id:'u2'}])
      assert.equal(isCurrentEmployeeRecord({...row,...patch}),false);
    assert.equal(isCurrentEmployeeRecord({employee_name:'동명이인',business_id:'b1',branch:'A'}),false);
    email=''; assert.equal(isCurrentEmployeeRecord({...row,employee_email:''}),true);
    requestId='';email='one@example.test';
    assert.equal(isCurrentEmployeeRecord({...row,employee_request_id:''}),true);
    employeeJoinRequests=[];assert.equal(isCurrentEmployeeRecord(row),false);
    ''')


def test_boot_entry_employee_admin_expired_and_token_preservation():
    run_js('''const assert=require('node:assert/strict');
    let authSession=null, payload, failed=false, cleared=false, redirected=false, employee=true;
    const AUTH_USER_KEY='auth', localStorage={setItem(){},removeItem(){}};
    const hasServerAuth=()=>true, financeApi=async()=>{if(failed)throw Object.assign(Error('401'),{status:401});return payload};
    const isEmployeeAccount=()=>employee, shouldOpenV41App=()=>!employee;
    const openV41App=()=>{redirected=true;return true},clearServerAuthToken=()=>{cleared=true};
    ''' + _function_source(HTML, 'verifyLegacyEntry') + '''
    (async()=>{
      payload={legacy_access:false};assert.equal(await verifyLegacyEntry(),true);
      employee=false;assert.equal(await verifyLegacyEntry(),false);assert.equal(redirected,true);
      failed=true;assert.equal(await verifyLegacyEntry(),true);assert.equal(cleared,true);assert.equal(authSession,null);
    })().catch(e=>{console.error(e);process.exitCode=1});
    ''')


def test_redirect_rejects_external_and_prefix_tricks():
    run_js("const assert=require('node:assert/strict');" + _function_source(HTML, 'safeRecipeRedirectPath') + '''
    for(const value of ['//evil.test','https://evil.test','/unni-naengmyeon/recipes-evil','/unni-naengmyeon/recipes\\\\evil'])
      assert.equal(safeRecipeRedirectPath(value),'');
    assert.equal(safeRecipeRedirectPath('/unni-naengmyeon/recipes?x=1#part'),'/unni-naengmyeon/recipes?x=1#part');
    ''')


def test_finance_api_null_and_204():
    run_js('''const assert=require('node:assert/strict');
    const assertEmployeePreviewReadOnly=()=>{},apiHeaders=()=>({});
    let status=200;
    const peopleFetch=async()=>({ok:true,status,json:async()=>{if(status===204)throw Error('empty');return null}});
    ''' + _function_source(HTML, 'financeApi') + '''
    (async()=>{assert.deepEqual(await financeApi('/session'),{});status=204;assert.deepEqual(await financeApi('/session'),{});})()
    .catch(e=>{console.error(e);process.exitCode=1});
    ''')


def test_join_query_encodes_business_once():
    run_js("""const assert=require('node:assert/strict');
    let employeeJoinRequests=[],called='';
    const hasServerAuth=()=>true,canManageOnboarding=()=>true;
    const selectedSettingsBusinessId=()=> 'company /&?';
    const financeApi=async path=>{called=path;return {requests:[]}};
    const renderEmployees=()=>{},renderOperationalPages=()=>{},renderOnboarding=()=>{};
    """ + _function_source(HTML, 'loadEmployeeJoinRequests') + """
    loadEmployeeJoinRequests().then(()=>{
      const url=new URL(called,'https://fixture.test');
      assert.equal(url.pathname,'/employees/join-requests');
      assert.equal(url.searchParams.get('business_id'),'company /&?');
    }).catch(e=>{console.error(e);process.exitCode=1});
    """)


def test_static_directory_manifest_and_sw_route_contract():
    main = (ROOT / 'app/yeoljeong_main.py').read_text()
    assert 'return FileResponse(_obys_index, media_type="text/html")' in main
    assert 'request.url.query' in main
    import json
    manifest = json.loads((ROOT / 'app/static/apps/obys/manifest-app.webmanifest').read_text())
    assert manifest['start_url'] == '/static/apps/obys/index.html'
    worker = (ROOT / 'app/static/apps/obys/sw.js').read_text()
    assert 'if (url.search) return;' in worker
    assert '"/static/apps/obys/index.html"' not in worker


def test_branchless_payroll_and_generated_required_documents_remain_scoped():
    sources = '\n'.join(_function_source(HTML, name) for name in (
        'isCurrentEmployeeRecord', 'visiblePayrollStatements',
        'mergeOnboardingMissingRows', 'visibleOnboardingDocuments', 'loadOnboardingDocuments'))
    run_js('''const assert=require('node:assert/strict');
    let employeePreview=null,authSession={user:{id:'u1',tenant_id:'t1'}};
    const employeeJoinRequests=[{id:'e1',email:'one@example.test',name:'합성',status:'approved',business_id:'b1',branch:'A'},
      {id:'e2',email:'other@example.test',status:'approved',business_id:'b1',branch:'A'}];
    const isEmployeeExperience=()=>true,currentEmployeeEmail=()=> 'one@example.test';
    const currentEmployeeName=()=> '합성',authPermissions=()=>({employee_request_id:'e1',branch:'A'});
    const requiredOnboardingDocumentTypes=()=>[{type:'identity',label:'신분증',requirement:'필수'}];
    const maskedEmail=()=> 'o***@example.test';
    let onboardingDocuments=[],payrollStatements=[];
    const hasServerAuth=()=>true,canManageOnboarding=()=>false;
    const financeApi=async()=>({documents:[]}),renderOnboarding=()=>{},renderEmployees=()=>{};
    ''' + sources + '''
    const row={id:'p1',employee_email:'one@example.test',business_id:'b1',status:'confirmed'};
    payrollStatements=[row,{...row,id:'p2',branch:'B'},{...row,id:'p3',business_id:'b2'},
      {...row,id:'p4',employee_email:'other@example.test'},{...row,id:'p5',business_id:''}];
    assert.deepEqual(visiblePayrollStatements().map(r=>r.id),['p1']);
    assert.equal(isCurrentEmployeeRecord(row),false,'branchless exception only for payroll');
    onboardingDocuments=mergeOnboardingMissingRows([],employeeJoinRequests);
    assert.equal(visibleOnboardingDocuments().length,1);
    assert.equal(visibleOnboardingDocuments()[0].business_id,'b1');
    (async()=>{await loadOnboardingDocuments();assert.equal(visibleOnboardingDocuments().length,1);
      assert.equal(visibleOnboardingDocuments()[0].business_id,'b1');
    })().catch(e=>{console.error(e);process.exitCode=1});
    ''')


def test_attendance_mapper_to_visible_shifts_preserves_identity():
    sources = '\n'.join(_function_source(HTML, name) for name in (
        'isCurrentEmployeeRecord', 'shiftFromServerRecord', 'visibleShifts', 'visibleContracts'))
    run_js('''const assert=require('node:assert/strict');
    let employeePreview=null,authSession={user:{id:'u1',tenant_id:'t1'}};
    let email='one@example.test',requestId='e1';
    let employeeJoinRequests=[{id:'e1',email,status:'approved',business_id:'b1',tenant_id:'t1',branch:'A'}];
    const isEmployeeExperience=()=>true,currentEmployeeEmail=()=>email;
    const authPermissions=()=>({employee_request_id:requestId});
    const shiftStatusFromServer={approved:'confirmed'};
    let rows=[],state={contracts:[]};const scopedShifts=()=>rows;
    ''' + sources + '''
    const raw={employee_email:email,employee_request_id:'e1',tenant_id:'t1',branch:'A',employee_name:'동명이인',status:'approved'};
    const mapped=patch=>shiftFromServerRecord({id:'s1',raw:{...raw,...patch}},'b1');
    rows=[mapped({}),mapped({employee_email:'other@example.test',employee_request_id:'e2'}),
      mapped({branch:'B'}),mapped({tenant_id:'t2'})];
    assert.equal(visibleShifts().length,1,'mapper output must retain own attendance');
    assert.equal(visibleShifts()[0].status,'confirmed');
    rows=[shiftFromServerRecord({id:'foreign',raw},'b2')];assert.equal(visibleShifts().length,0);
    email='';employeeJoinRequests[0].email='';rows=[mapped({employee_email:''})];
    assert.equal(visibleShifts().length,1,'no-email employee identified by request id');
    state.contracts=[{business_id:'b1',branch:'A',tenant_id:'t1',employee_request_id:'e1'}];
    assert.equal(visibleContracts().length,1);
    email='one@example.test';requestId='';employeeJoinRequests[0].email=email;
    rows=[mapped({employee_request_id:''})];assert.equal(visibleShifts().length,1,'legacy email approval');
    rows=[mapped({employee_request_id:'',employee_email:'',employee_name:'동명이인'})];
    assert.equal(visibleShifts().length,0,'name-only association forbidden');
    ''')


def test_required_documents_converge_for_both_parallel_response_orders():
    sources = '\n'.join(_function_source(HTML, name) for name in (
        'isCurrentEmployeeRecord', 'mergeOnboardingMissingRows', 'visibleOnboardingDocuments',
        'loadOnboardingDocuments', 'loadEmployeeJoinRequests', 'loadOnboardingDocumentTypes'))
    run_js('''const assert=require('node:assert/strict');
    let employeePreview=null,authSession={user:{id:'u1',tenant_id:'t1'}};
    let employeeJoinRequests=[],onboardingDocuments=[],onboardingDocumentTypes=[];
    let email='one@example.test',requestId='e1';
    const isEmployeeExperience=()=>true,currentEmployeeEmail=()=>email;
    const authPermissions=()=>({employee_request_id:requestId});
    const hasServerAuth=()=>true,canManageOnboarding=()=>false;
    const renderOnboarding=()=>{},renderEmployees=()=>{},renderOperationalPages=()=>{},initSelects=()=>{};
    const requiredOnboardingDocumentTypes=()=>onboardingDocumentTypes.length?onboardingDocumentTypes:[{type:'identity',label:'신분증',requirement:'필수'}];
    const maskedEmail=()=> 'o***@example.test';
    let pending={};const financeApi=path=>new Promise(resolve=>{pending[path]=resolve});
    ''' + sources + '''
    const own={id:'e1',email,name:'합성',status:'approved',business_id:'b1',tenant_id:'t1',branch:'A'};
    (async()=>{
      for(const first of ['documents','requests']){
        employeeJoinRequests=[];onboardingDocuments=[];pending={};
        const docs=loadOnboardingDocuments(),joins=loadEmployeeJoinRequests();
        const resolveDocs=()=>pending['/onboarding/documents']({documents:[]});
        const resolveJoins=()=>pending['/employees/join-requests']({requests:[own,{...own,id:'e2',email:'other@example.test'},{...own,id:'foreign',business_id:'b2',email:'other@example.test'}]});
        if(first==='documents'){resolveDocs();await docs;resolveJoins();await joins}
        else{resolveJoins();await joins;resolveDocs();await docs}
        let rows=visibleOnboardingDocuments();assert.equal(rows.length,1,first);
        assert.equal(rows[0].business_id,'b1');assert.equal(rows[0].tenant_id,'t1');
        const foreign=loadOnboardingDocuments();pending['/onboarding/documents']({documents:[{id:'foreign-doc',employee_email:email,business_id:'b2',tenant_id:'t2',branch:'A',document_type:'identity'}]});await foreign;
        rows=visibleOnboardingDocuments();assert.equal(rows.length,1,'foreign-company upload cannot satisfy required docs');
        assert.equal(rows[0].is_placeholder,true);
        // Re-read, uploaded document replaces placeholder; repeated reads cannot duplicate it.
        const again=loadOnboardingDocuments();pending['/onboarding/documents']({documents:[{...own,id:'doc1',employee_request_id:'e1',employee_email:email,document_type:'identity',status:'approved'}]});await again;
        rows=visibleOnboardingDocuments();assert.equal(rows.length,1);assert.equal(rows[0].id,'doc1');
        email='';employeeJoinRequests=[{...own,email:''}];onboardingDocuments=[];
        rows=visibleOnboardingDocuments();assert.equal(rows.length,1,'no email required docs');
        email='one@example.test';
        const types=loadOnboardingDocumentTypes();pending['/onboarding/document-types']({document_types:[{type:'bank',label:'통장',requirement:'필수'}]});await types;
        assert.equal(visibleOnboardingDocuments()[0].document_type,'bank');
        onboardingDocumentTypes=[];
      }
    })().catch(e=>{console.error(e);process.exitCode=1});
    ''')


def test_multiple_approved_placements_use_matching_assignment_identity():
    sources = '\n'.join(_function_source(HTML, name) for name in (
        'isCurrentEmployeeRecord', 'visibleContracts', 'visiblePayrollStatements'))
    run_js('''const assert=require('node:assert/strict');
    let employeePreview=null,authSession={user:{id:'u1',tenant_id:'t1'}};
    let email='one@example.test';
    const isEmployeeExperience=()=>true,currentEmployeeEmail=()=>email;
    const authPermissions=()=>({employee_request_id:'e1'});
    const a={id:'e1',email,status:'approved',business_id:'b1',tenant_id:'t1',branch:'A'};
    const b={...a,id:'e2',branch:'B'};
    let employeeJoinRequests=[a,b,{...a,id:'e3',branch:'C',status:'pending'},
      {...a,id:'e4',branch:'D',email:'other@example.test'},
      {...a,id:'e5',tenant_id:'t2',business_id:'foreign'}];
    ''' + sources + '''
    const row={employee_request_id:'e2',employee_email:email,business_id:'b1',tenant_id:'t1',branch:'B'};
    let state={contracts:[row]},payrollStatements=[row];
    assert.equal(isCurrentEmployeeRecord(row),true,'second approved placement');
    assert.equal(visibleContracts().length,1);assert.equal(visiblePayrollStatements().length,1);
    assert.equal(isCurrentEmployeeRecord({...row,branch:'A'}),false,'placement id must match branch');
    for(const patch of [{employee_request_id:'e3',branch:'C'}, {employee_request_id:'e4',branch:'D'},
      {employee_request_id:'e5',business_id:'foreign',tenant_id:'t2'}, {employee_email:'other@example.test'},
      {employee_request_id:'unknown'}, {employee_request_id:'e2',business_id:'foreign'}])
      assert.equal(isCurrentEmployeeRecord({...row,...patch}),false);
    employeeJoinRequests=[{...a,id:'e2',branch:'B'}];
    assert.equal(isCurrentEmployeeRecord(row),true,'session assignment need not be returned');
    email='';employeeJoinRequests=[{...b,email:'',user_id:'u1'}];
    assert.equal(isCurrentEmployeeRecord({...row,employee_email:'',user_id:'u1'}),true,'email-less second placement verified by user id');
    employeeJoinRequests[0].user_id='u2';
    assert.equal(isCurrentEmployeeRecord({...row,employee_email:'',user_id:'u1'}),false,'other user assignment blocked');
    ''')


def test_rejected_document_resubmission_survives_both_response_orders():
    sources = '\n'.join(_function_source(HTML, name) for name in (
        'isCurrentEmployeeRecord', 'mergeOnboardingMissingRows', 'visibleOnboardingDocuments',
        'loadOnboardingDocuments', 'loadEmployeeJoinRequests'))
    run_js('''const assert=require('node:assert/strict');
    let employeePreview=null,authSession={user:{id:'u1',tenant_id:'t1'}};
    let employeeJoinRequests=[],onboardingDocuments=[];
    const email='one@example.test',currentEmployeeEmail=()=>email,isEmployeeExperience=()=>true;
    const authPermissions=()=>({employee_request_id:'e1'});
    const hasServerAuth=()=>true,canManageOnboarding=()=>false;
    const renderOnboarding=()=>{},renderEmployees=()=>{},renderOperationalPages=()=>{};
    const requiredOnboardingDocumentTypes=()=>[{type:'identity',label:'신분증',requirement:'필수'}];
    const maskedEmail=()=> 'o***@example.test';
    let pending={};const financeApi=path=>new Promise(resolve=>{pending[path]=resolve});
    ''' + sources + '''
    const own={id:'e1',email,status:'approved',business_id:'b1',tenant_id:'t1',branch:'A'};
    const rejected={...own,id:'rejected-doc',employee_request_id:'e1',employee_email:email,
      document_type:'identity',status:'rejected',review_memo:'합성 보완 사유'};
    const resubmit={...rejected,id:'server-resubmit',status:'resubmit_required',
      status_label:'재제출 필요',is_placeholder:true,missing_document:true,previous_document_id:'rejected-doc'};
    (async()=>{
      for(const first of ['documents','requests']){
        employeeJoinRequests=[];onboardingDocuments=[];pending={};
        const docs=loadOnboardingDocuments(),joins=loadEmployeeJoinRequests();
        const resolveDocs=()=>pending['/onboarding/documents']({documents:[rejected,resubmit]});
        const resolveJoins=()=>pending['/employees/join-requests']({requests:[own]});
        if(first==='documents'){resolveDocs();await docs;resolveJoins();await joins}
        else{resolveJoins();await joins;resolveDocs();await docs}
        const rows=visibleOnboardingDocuments();
        assert.equal(rows.length,2,first);
        const needed=rows.filter(row=>row.missing_document);
        assert.equal(needed.length,1);assert.equal(needed[0].id,'server-resubmit');
        assert.equal(needed[0].previous_document_id,'rejected-doc');
        assert.equal(needed[0].review_memo,'합성 보완 사유');
        assert.deepEqual(visibleOnboardingDocuments(),rows,'render is idempotent');
        onboardingDocuments=[rejected];
        const rebuilt=visibleOnboardingDocuments().find(row=>row.missing_document);
        assert.equal(rebuilt.status,'resubmit_required');assert.equal(rebuilt.previous_document_id,'rejected-doc');
        onboardingDocuments=[rejected,{...rejected,id:'new-upload',status:'uploaded'},resubmit];
        assert.equal(visibleOnboardingDocuments().filter(row=>row.missing_document).length,0,'new upload resolves resubmission');
      }
    })().catch(e=>{console.error(e);process.exitCode=1});
    ''')


def test_resubmission_entry_selects_document_and_blocks_preview():
    run_js('''const assert=require('node:assert/strict');
    let employeePreview=null,authed=true,focused=null,view='';
    const field=()=>({value:''});
    const form={employeeName:field(),employeeEmail:field(),branch:field(),documentType:field(),file:field()};
    const els={onboardingUploadForm:form},hasServerAuth=()=>authed;
    const currentEmployeeName=()=> '합성 직원',currentEmployeeEmail=()=> 'one@example.test';
    const visibleOnboardingDocuments=()=>[{id:'resubmit',employee_name:'합성 직원',employee_email:'one@example.test',
      branch:'B',document_type:'identity',missing_document:true,status:'resubmit_required'}];
    const setView=value=>view=value,suggestOnboardingExpiry=()=>{},focusElement=value=>focused=value;
    ''' + _function_source(HTML, 'startOnboardingDocument') + '''
    assert.equal(startOnboardingDocument('resubmit'),true);
    assert.equal(view,'onboarding');assert.equal(form.branch.value,'B');
    assert.equal(form.documentType.value,'identity');assert.equal(focused,form.file);
    assert.equal(startOnboardingDocument('foreign'),false);
    employeePreview={email:'one@example.test'};assert.equal(startOnboardingDocument('resubmit'),false);
    employeePreview=null;authed=false;assert.equal(startOnboardingDocument('resubmit'),false);
    ''')


def test_home_document_tile_keeps_other_placement_resubmission_pending():
    portal = (ROOT / 'app/static/apps/obys/modules/employee-portal.js').read_text()
    run_js('''const assert=require('node:assert/strict');
    ''' + _function_source(portal, 'onboardingTile') + '''
    const required=[{type:'identity',label:'신분증'}];
    const uploaded={document_type:'identity',status:'approved',branch:'A'};
    const resubmit={document_type:'identity',status:'resubmit_required',branch:'B',is_placeholder:true,missing_document:true};
    assert.equal(onboardingTile([uploaded,resubmit],required).pc,'0 / 1 제출');
    assert.equal(onboardingTile([uploaded],required).pc,'1 / 1 제출');
    assert.equal(onboardingTile([{...uploaded,status:'needs_fix'}],required).pc,'0 / 1 제출');
    ''')


def test_preview_uses_selected_approved_assignment_without_email_or_name_matching():
    source = _function_source(HTML, 'isCurrentEmployeeRecord')
    run_js('''const assert=require('node:assert/strict');
    let employeePreview={email:'',name:'same name',requestId:'e2'};
    let authSession={user:{id:'admin',tenant_id:'t1'}};
    let employeeJoinRequests=[
      {id:'e1',name:'same name',status:'approved',business_id:'b1',branch:'A'},
      {id:'e2',name:'same name',status:'approved',business_id:'b1',branch:'B'},
      {id:'e3',name:'same name',status:'pending',business_id:'b1',branch:'C'}];
    const isEmployeeExperience=()=>true,currentEmployeeEmail=()=>'',authPermissions=()=>({});
    ''' + source + '''
    const row={employee_request_id:'e2',business_id:'b1',branch:'B',tenant_id:'t1'};
    assert.equal(isCurrentEmployeeRecord(row),true);
    for(const patch of [{employee_request_id:'e1'},{business_id:'b2'},{branch:'A'},{tenant_id:'t2'}])
      assert.equal(isCurrentEmployeeRecord({...row,...patch}),false);
    employeePreview.requestId='e3';assert.equal(isCurrentEmployeeRecord({...row,employee_request_id:'e3',branch:'C'}),false);
    employeePreview.requestId='';assert.equal(isCurrentEmployeeRecord(row),false);
    ''')


def test_boot_transient_failure_preserves_credentials_and_requires_retry():
    run_js('''const assert=require('node:assert/strict');
    let authSession={user:{id:'u1'}},cleared=false,rendered='',button={};
    const original=authSession;
    const AUTH_USER_KEY='auth',localStorage={removeItem(){cleared=true}};
    const hasServerAuth=()=>true,clearServerAuthToken=()=>{cleared=true};
    let failure=Error('network'); const financeApi=async()=>{throw failure};
    const document={body:{insertAdjacentHTML:(where,html)=>{rendered=html}},getElementById:()=>button};
    let reloaded=false; const window={location:{reload(){reloaded=true}}};
    ''' + _function_source(HTML, 'verifyLegacyEntry') + '''
    (async()=>{
      for(const error of [Error('network'),Object.assign(Error('busy'),{status:503}),Object.assign(Error('forbidden'),{status:403})]) {
        failure=error;assert.equal(await verifyLegacyEntry(),false);
        assert.equal(cleared,false);assert.equal(authSession,original);
        assert.match(rendered,/다시 연결/);button.onclick();assert.equal(reloaded,true);
      }
    })().catch(e=>{console.error(e);process.exitCode=1});
    ''')


def test_login_waits_for_handler_and_never_defaults_to_get():
    import re

    form = re.search(r'<form id="gateLoginForm"[^>]*>', HTML).group(0)
    assert 'method="post"' in form
    assert 'onsubmit="event.preventDefault()"' in form
    assert re.search(r'<button id="gateLoginSubmitBtn"[^>]* disabled>', HTML)
    bind = _function_source(HTML, 'bindEvents')
    assert bind.index('els.gateLoginForm.addEventListener("submit"') < bind.index('getElementById("gateLoginSubmitBtn").disabled = false')


def test_nonlegacy_admin_preview_does_not_load_global_legacy_settings():
    run_js('''const assert=require('node:assert/strict');
    let authSession={legacy_access:false},employee=false,calls=0;
    const hasServerAuth=()=>true,isEmployeeAccount=()=>employee,canManageSettings=()=>true;
    const financeApi=async()=>{calls++;return {settings:{}}};
    const hasMeaningfulSettings=()=>false;
    ''' + _function_source(HTML, 'loadServerSettings') + '''
    (async()=>{
      assert.equal(await loadServerSettings(),null);assert.equal(calls,0);
      authSession.legacy_access=true;await loadServerSettings();assert.equal(calls,1);
      employee=true;await loadServerSettings();assert.equal(calls,1);
    })().catch(e=>{console.error(e);process.exitCode=1});
    ''')
    migration = _function_source(HTML, 'autoMigrateLocalShifts')
    assert 'authSession?.legacy_access === false' in migration
