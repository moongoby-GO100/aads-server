"""오비서 index.html 직원 화면 — 남의 회사 매장 노출·소유자 API 호출·이메일 이름 표시 회귀 방지.

D1: defaultSettings 에 특정 회사의 실매장을 두지 않고, 서버 사업자 목록을 받기 전에는 어떤 매장명도 그리지 않는다.
D2: 직원 계정(과 관리자 직원 미리보기)은 소유자용 매출·정산·수집·재고 API 를 부르지 않고 오버뷰 모듈도 싣지 않는다(모듈 파일은 건드리지 않는다).
D3: 인사말 이름은 직원 기록 name → 계정 name → 이메일 @ 앞부분 순서이고 전체 이메일을 내보내지 않는다.

서버 권한 로직은 건드리지 않는다. 원문 함수를 뽑아 node 로 실행하는 검증은 node 가 없으면 건너뛴다.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node 미설치 런타임")

REAL_STORE_TOKENS = ["열정국밥", "중화점", "성신여대", "미아점", "biz-junghwa", "biz-sungshin", "biz-mia"]
OWNER_ONLY_LOADERS = [
    "loadDeliveryLedgers",
    "loadDeliverySettlements",
    "loadDeliverySales",
    "loadDeliveryReviews",
    "loadCollectionStatus",
    "loadSalesLedger",
    "loadInventoryData",
]


def _function_source(name: str) -> str:
    found = re.search(rf"^[ \t]*(?:async )?function {re.escape(name)}\(", INDEX, re.MULTILINE)
    assert found, name
    start = found.start()
    open_at = INDEX.index(") {", start) + 2
    depth = 0
    for pos in range(open_at, len(INDEX)):
        char = INDEX[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return INDEX[start : pos + 1]
    raise AssertionError(name)


def _between(begin: str, end: str) -> str:
    head = INDEX.index(begin)
    return INDEX[head : INDEX.index(end, head)]


def _run_node(body: str, tmp_path) -> dict:
    script = tmp_path / "harness.cjs"
    script.write_text(body, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(script)], check=False, text=True, capture_output=True, timeout=30,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestD1NoOtherCompanyStores:
    def test_default_settings_has_no_real_store(self):
        block = _between("const defaultSettings = {", "const LEGACY_SEED_BUSINESS_IDS")
        for token in REAL_STORE_TOKENS:
            assert token not in block, token
        assert re.search(r"businesses:\s*\[\]", block)
        assert re.search(r"branches:\s*\[\]", block)
        assert re.search(r"accounts:\s*\[\]", block)

    def test_sample_data_has_no_store_rows(self):
        block = _between("const sampleData = {", "let state = loadState();")
        assert re.search(r"entries:\s*\[\]", block)
        for token in REAL_STORE_TOKENS:
            assert token not in block, token

    def test_no_mia_fallback_reassignment(self):
        assert "MIA_BUSINESS_ID" not in INDEX
        assert "MIA_BRANCH_NAME" not in INDEX
        assert "CANONICAL_BUSINESS_IDS" not in INDEX
        assert "normalizeMiaBusinessLinks(" in _function_source("mergeSettings")

    @needs_node
    def test_existing_link_normalizer_is_kept_and_tenant_neutral(self, tmp_path):
        script = (
            'const DELIVERY_PORTAL_SERVICES=new Set(["baemin","coupangeats","yogiyo","ddangyo"]);\n'
            + _function_source("normalizeMiaBusinessLinks")
            + '\nconst mk=()=>({businesses:[{id:"biz-own"}],branches:[{name:"본점",businessId:"biz-own"}],'
            'integrations:[{service:"baemin",branch:"본점",businessId:"",business_id:""},'
            '{service:"yogiyo",branch:"",businessId:"biz-own",business_id:""},'
            '{service:"coupangeats",branch:"",businessId:"",business_id:""},'
            '{service:"homtax",branch:"본점",businessId:"",business_id:""}]});\n'
            'const r=normalizeMiaBusinessLinks(mk());\n'
            'const empty=normalizeMiaBusinessLinks({businesses:[],branches:[],integrations:[{service:"baemin",branch:"",businessId:"",business_id:""}]});\n'
            'console.log(JSON.stringify({ids:r.integrations.map(i=>i.businessId),legacy:r.integrations.map(i=>i.business_id),empty:empty.integrations[0]}));\n'
        )
        out = _run_node(script, tmp_path)
        assert out["ids"] == ["biz-own", "biz-own", "", ""]
        assert out["legacy"] == ["biz-own", "biz-own", "", ""]
        assert out["empty"]["businessId"] == "" and out["empty"]["business_id"] == ""

    def test_no_hardcoded_branch_fallback_in_code(self):
        code = INDEX[INDEX.index("<script>") :]
        assert "열정국밥_미아점" not in code
        assert '|| "중화점"' not in code

    def test_seed_ids_only_appear_in_the_legacy_purge_list(self):
        for token in ["biz-junghwa", "biz-sungshin", "biz-mia"]:
            lines = [line for line in INDEX.splitlines() if token in line]
            assert len(lines) == 1, (token, lines)
            assert "LEGACY_SEED_BUSINESS_IDS" in lines[0]

    @needs_node
    def test_stored_legacy_seed_is_dropped_but_server_rows_survive(self, tmp_path):
        script = (
            'const out={};\n'
            'const LEGACY_SEED_BUSINESS_IDS=new Set(["biz-junghwa","biz-sungshin","biz-mia"]);\n'
            'const LEGACY_SEED_BRANCH_IDS=new Set(["branch-junghwa","branch-sungshin","branch-gangbuk-mia"]);\n'
            'const LEGACY_SEED_ACCOUNT_IDS=new Set(["acct-sungshin-ibk"]);\n'
            + _function_source("dropLegacySeedSettings")
            + '\nconst stored={businesses:[{id:"biz-junghwa",name:"X"},{id:"biz-own",name:"내 매장"}],'
            'branches:[{id:"branch-junghwa",businessId:"biz-junghwa"},{id:"b-own",businessId:"biz-own"}],'
            'accounts:[{id:"acct-sungshin-ibk"},{id:"a-own"}],'
            'integrations:[{id:"int-baemin",businessId:"biz-mia",branch:"열정국밥_미아점"},{id:"int-own",businessId:"biz-own",branch:"지점"}]};\n'
            'const r=dropLegacySeedSettings(stored);\n'
            'out.businesses=r.businesses.map(x=>x.id);out.branches=r.branches.map(x=>x.id);out.accounts=r.accounts.map(x=>x.id);\n'
            'out.integrations=r.integrations;out.passthrough=dropLegacySeedSettings(null);\n'
            'console.log(JSON.stringify(out));\n'
        )
        out = _run_node(script, tmp_path)
        assert out["businesses"] == ["biz-own"]
        assert out["branches"] == ["b-own"]
        assert out["accounts"] == ["a-own"]
        assert out["integrations"][0]["businessId"] == ""
        assert out["integrations"][0]["branch"] == ""
        assert out["integrations"][1]["businessId"] == "biz-own"
        assert out["passthrough"] is None

    def test_loading_failure_retry_state_is_wired(self):
        assert 'id="businessListState"' in INDEX
        render = _function_source("renderBusinessListState")
        assert "사업자 목록을 불러오는 중입니다" in render
        assert "사업자 목록을 불러오지 못했습니다" in render
        assert "다시 시도" in render
        assert "retryBusinessList" in render
        assert "loadServerSettings()" in _function_source("retryBusinessList")
        assert "renderBusinessListState();" in _function_source("renderAll")

    def test_every_entry_point_goes_through_the_status_tracking_loader(self):
        # 세 진입점(init·로그인·가입)의 기존 호출 모양은 그대로이고, 상태 추적은 loadServerSettings 한 곳에 있다.
        assert INDEX.count("loadServerSettings().catch(err => showToast(`기초설정 불러오기 실패: ${err.message}`)).then(") == 3
        assert INDEX.count(".then(() => autoMigrateLocalShifts())") == 3
        body = _function_source("trackBusinessListLoad")
        for token in ('setBusinessListStatus("loading")', 'setBusinessListStatus("failed"', 'setBusinessListStatus("ready")'):
            assert token in body, token
        assert "loadServerSettings = trackBusinessListLoad(loadServerSettings);" in INDEX
        assert "setBusinessListStatus" not in _function_source("loadServerSettings")
        assert "await loadServerSettings();" in _function_source("reloadDataAfterTenantSwitch")

    def test_ready_requires_a_real_business_list(self):
        body = _function_source("trackBusinessListLoad")
        assert body.index("if (!payload)") < body.index('setBusinessListStatus("ready")')
        assert "payload.settings?.businesses" in body
        assert body.index("payload.settings?.businesses") < body.index('setBusinessListStatus("ready")')

    def test_ledgers_wait_for_server_business_list(self):
        init = _between("async function init()", "init().catch(")
        for name in ("loadDeliveryLedgers", "loadSalesLedger", "loadInventoryData"):
            assert name not in init, name
        assert "loadOwnerData();" in _function_source("settleBusinessList")

    def test_business_scoped_calls_skip_when_no_business(self):
        assert "!businessId" in _function_source("loadLedgerUploads")
        assert "ownerBusinessApisAllowed()" in _function_source("loadLedgerUploads")
        refresh = _function_source("refreshBankAccounts")
        assert refresh.index("ownerBusinessApisAllowed()") < refresh.index("financeApi(")
        assert refresh.index("!scope.business_id") < refresh.index("financeApi(")

    def test_home_waits_for_business_list(self):
        body = _function_source("loadTodayHome")
        assert body.index("ownerBusinessApisAllowed()") < body.index("financeApi(")

    @needs_node
    @pytest.mark.parametrize(
        "outcome,expected_status",
        [
            ("fail", "failed"),  # 네트워크·서버 오류
            ("swallowed403", "failed"),  # 원본 loadServerSettings 가 403 을 삼키고 null 을 돌려줌 — 리뷰 지적 1
            ("emptyList", "failed"),  # 응답은 왔지만 사업자가 없음
            ("ok", "ready"),
        ],
    )
    def test_owner_calls_only_after_a_real_business_list(self, tmp_path, outcome, expected_status):
        names = [
            "ownerApisAllowed", "ownerBusinessApisAllowed", "isEmployeeAccount", "isEmployeeExperience",
            "setBusinessListStatus", "businessListFailureText", "renderBusinessListState",
            "trackBusinessListLoad", "loadOwnerData", "ensureOwnerEntryModules", "settleBusinessList",
            "loadDeliveryLedgers", "loadDeliverySettlements", "loadDeliverySales", "loadDeliveryReviews",
            "loadCollectionStatus", "loadSalesLedger", "loadInventoryData",
        ]
        script = (
            'const calls=[];let employeePreview=null;let currentView="salesLedger";\n'
            'const hasServerAuth=()=>true;const authPermissions=()=>({role:"owner"});\n'
            'const canManageAutomation=()=>false;const showToast=()=>{};const els={};const state={settings:{businesses:[]}};\n'
            'const businessListStatus={state:"idle",message:""};\n'
            'const window={};const loadLegacyEntryModules=async()=>{calls.push("modules")};\n'
            'const financeApi=async p=>{calls.push(p);return {statuses:[],settlements:[],sales:[],reviews:[]}};\n'
            'const inventoryApi=async p=>{calls.push(p);return {}};\n'
            'const selectedSettingsBusinessId=()=>"";let dbCollectionStatuses=[];const renderAll=()=>{};\n'
            'const loadTodayHome=async()=>{};\n'
            f'const outcome={json.dumps(outcome)};\n'
            'let loadServerSettings=async()=>{ if(outcome==="fail"){const e=new Error("503");e.status=503;throw e;}'
            ' if(outcome==="swallowed403")return null;'
            ' return {settings:{businesses: outcome==="emptyList"?[]:[{id:"biz-own"}]}}; };\n'
            + "\n".join(_function_source(n) for n in names)
            + '\nloadServerSettings=trackBusinessListLoad(loadServerSettings);\n(async()=>{\n'
            '  await loadSalesLedger({silent:true});await loadInventoryData({silent:true});await loadDeliveryLedgers({silent:true});\n'
            '  const before=calls.length;\n'
            '  try{await loadServerSettings();}catch(_){}\n'
            '  await new Promise(r=>setTimeout(r,20));\n'
            '  console.log(JSON.stringify({before,after:calls.length,status:businessListStatus.state,calls}));\n'
            '})().catch(e=>{console.error(e);process.exit(1)});\n'
        )
        out = _run_node(script, tmp_path)
        assert out["before"] == 0, out
        assert out["status"] == expected_status, out
        if expected_status == "failed":
            assert out["after"] == 0, out
        else:
            assert any("/settlements" in c for c in out["calls"]), out
            assert any("/sales" in c for c in out["calls"]), out


class TestD2EmployeeSkipsOwnerApis:
    def test_gate_reuses_existing_role_functions(self):
        gate = _function_source("ownerApisAllowed")
        assert "hasServerAuth()" in gate
        assert "isEmployeeExperience()" in gate

    @pytest.mark.parametrize("name", OWNER_ONLY_LOADERS)
    def test_every_owner_loader_starts_with_the_gate(self, name):
        body = _function_source(name)
        first = [line.strip() for line in body.splitlines()[1:3] if line.strip()][0]
        assert first == "if (!ownerBusinessApisAllowed()) return;", (name, first)

    def test_employee_does_not_load_overview_modules_at_init(self):
        # 2026-11-04 검수 지적: 소유자도 init 에서 싣지 않는다(사업자 목록 ready 후 settleBusinessList 가 싣는다).
        init = _between("async function init()", "init().catch(")
        assert "loadLegacyEntryModules()" not in init

    def test_modules_are_loaded_late_for_owner_login_after_employee_session(self):
        # 리뷰 지적 2: 직원으로 열린 페이지에서 소유자로 로그인해도 모듈이 실려야 한다.
        # 2026-11-04 검수 지적: 로그인 직후에도 사업자 목록 ready 전에는 싣지 않고, settleBusinessList 가 싣는다.
        login = _function_source("loginToServer")
        assert "ensureOwnerEntryModules()" not in login
        assert login.index("verifyLegacyEntry()") < login.index("loadServerSettings()")
        assert "ensureOwnerEntryModules()" in _function_source("settleBusinessList")

    @needs_node
    @pytest.mark.parametrize(
        "role,preview,expected_loads",
        [("employee", False, 0), ("admin", True, 0), ("owner", False, 1)],
    )
    def test_late_module_load_follows_current_role(self, tmp_path, role, preview, expected_loads):
        sources = "\n".join(_function_source(n) for n in ["isEmployeeAccount", "isEmployeeExperience", "ensureOwnerEntryModules"])
        script = (
            f'let role={json.dumps(role)};let employeePreview={"{}" if preview else "null"};let loads=0;\n'
            'const authPermissions=()=>({role});const showToast=()=>{};\n'
            'const loadLegacyEntryModules=async()=>{loads+=1};\n'
            + sources
            + '\n(async()=>{await ensureOwnerEntryModules();'
            'const afterFirst=loads;role="owner";employeePreview=null;await ensureOwnerEntryModules();'
            'console.log(JSON.stringify({afterFirst,afterOwner:loads}));})().catch(e=>{console.error(e);process.exit(1)});\n'
        )
        out = _run_node(script, tmp_path)
        assert out["afterFirst"] == expected_loads
        assert out["afterOwner"] == expected_loads + 1  # 같은 페이지에서 소유자로 바뀌면 그때 싣는다

    def test_owner_data_is_gated_in_one_place(self):
        body = _function_source("loadOwnerData")
        assert body.index("ownerBusinessApisAllowed()") < body.index("loadDeliveryLedgers(")

    def test_home_still_skips_employee(self):
        assert "if (isEmployeeExperience()) return;" in _function_source("loadTodayHome")

    @needs_node
    @pytest.mark.parametrize(
        "role,preview,expected_calls",
        [("employee", False, 0), ("employee_pending", False, 0), ("admin", True, 0), ("owner", False, 1)],
    )
    def test_loaders_do_not_call_owner_endpoints_for_employee(self, tmp_path, role, preview, expected_calls):
        sources = "\n".join(
            _function_source(name)
            for name in [
                "ownerApisAllowed", "ownerBusinessApisAllowed", "isEmployeeAccount", "isEmployeeExperience",
                *OWNER_ONLY_LOADERS,
            ]
        )
        driver = (
            "await loadCollectionStatus();"
            if role == "owner"
            else "await loadCollectionStatus();await loadDeliverySettlements();await loadDeliverySales();"
            "await loadDeliveryReviews();await loadDeliveryLedgers({silent:true});await loadSalesLedger({silent:true});"
            "await loadInventoryData({silent:true});"
        )
        script = (
            f'const role={json.dumps(role)};let employeePreview={"{}" if preview else "null"};\n'
            'const calls=[];\n'
            'const businessListStatus={state:"ready",message:""};\n'
            'const hasServerAuth=()=>true;\n'
            'const authPermissions=()=>({role});\n'
            'const financeApi=async p=>{calls.push(p);return {statuses:[]}};\n'
            'const inventoryApi=async p=>{calls.push(p);return {}};\n'
            'const selectedSettingsBusinessId=()=>"";\n'
            'let dbCollectionStatuses=[];const renderAll=()=>{};\n'
            + sources
            + "\n(async()=>{" + driver + "console.log(JSON.stringify({calls}));})()"
            ".catch(e=>{console.error(e);process.exit(1)});\n"
        )
        out = _run_node(script, tmp_path)
        # 소유자 대조군은 같은 코드가 역할 때문에 막힌 것임을 보인다.
        assert len(out["calls"]) == expected_calls, out["calls"]


class TestD3EmployeeDisplayName:
    def test_no_raw_email_fallback(self):
        body = _function_source("currentEmployeeName")
        assert "|| email\n" not in body and "|| email;" not in body
        assert 'includes("@")' in body

    @needs_node
    def test_name_fallback_order(self, tmp_path):
        sources = "\n".join(_function_source(name) for name in ["currentEmployeeEmail", "currentEmployeeName"])
        script = (
            'let employeePreview=null;let authSession=null;let employeeJoinRequests=[];let permissions={};\n'
            'let selfSignup={};\n'
            'const authPermissions=()=>permissions;const loadEmployeeSelfSignupMap=()=>selfSignup;\n'
            + sources
            + '\nconst out={};\n'
            'const user=(extra={})=>({user:{email:"Kim.Staff@aads.dev",...extra}});\n'
            '// 1) 직원 기록 name 이 최우선\n'
            'authSession=user({name:"계정이름"});permissions={employee_request_id:"r1"};\n'
            'employeeJoinRequests=[{id:"r1",status:"approved",name:"기록이름",email:"kim.staff@aads.dev"}];\n'
            'out.record=currentEmployeeName();\n'
            '// 2) 기록 없으면 계정 name\n'
            'employeeJoinRequests=[];out.account=currentEmployeeName();\n'
            '// 3) 계정 name 이 이메일이면 건너뛰고 가입 시 이름\n'
            'authSession=user({name:"Kim.Staff@aads.dev"});selfSignup={"kim.staff@aads.dev":{name:"가입이름"}};out.signup=currentEmployeeName();\n'
            '// 4) 아무 이름도 없으면 이메일 @ 앞부분\n'
            'selfSignup={};authSession=user({});out.local=currentEmployeeName();\n'
            'authSession=user({name:""});employeeJoinRequests=[{id:"r9",status:"approved",name:"kim.staff@aads.dev",email:"kim.staff@aads.dev"}];\n'
            'out.emailNamedRecord=currentEmployeeName();\n'
            '// 5) 승인 전 기록은 쓰지 않는다\n'
            'employeeJoinRequests=[{id:"r1",status:"pending",name:"대기이름",email:"kim.staff@aads.dev"}];permissions={employee_request_id:"r1"};\n'
            'out.pending=currentEmployeeName();\n'
            '// 6) 관리자 미리보기는 직원 기록명, 없으면 이메일 앞부분\n'
            'employeePreview={email:"emp@example.com",name:"홍길동"};out.preview=currentEmployeeName();\n'
            'employeePreview={email:"emp@example.com",name:""};out.previewNoName=currentEmployeeName();\n'
            'employeePreview={email:"",name:""};out.previewEmpty=currentEmployeeName();\n'
            'console.log(JSON.stringify(out));\n'
        )
        out = _run_node(script, tmp_path)
        assert out["record"] == "기록이름"
        assert out["account"] == "계정이름"
        assert out["signup"] == "가입이름"
        assert out["local"] == "kim.staff"
        assert out["emailNamedRecord"] == "kim.staff"
        assert out["pending"] == "kim.staff"
        assert out["preview"] == "홍길동"
        assert out["previewNoName"] == "emp"
        assert out["previewEmpty"] == "직원"
        assert all("@" not in value for value in out.values())


@needs_node
def test_inline_scripts_pass_node_check(tmp_path):
    scripts = re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"application/obys-deferred\")[^>]*>(.*?)</script>", INDEX, re.S)
    assert scripts
    for number, body in enumerate(scripts):
        target = tmp_path / f"inline_{number}.js"
        target.write_text(body, encoding="utf-8")
        result = subprocess.run([NODE, "--check", str(target)], check=False, text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr


# ── runner-c131887c 검수 지적 반영(2026-11-04): 개요 모듈 선로딩 우회, 실패 후 사업자 추가 시 복구 ──


def test_init_does_not_load_overview_modules_before_business_list_ready():
    # data-overview.js 는 로드 즉시 /today-overview·/status 를 부르므로 init 에서 먼저 싣지 않는다.
    init_body = _function_source("init")
    assert "loadLegacyEntryModules" not in init_body
    settle = _function_source("settleBusinessList")
    assert settle.index("ownerBusinessApisAllowed()") < settle.index("ensureOwnerEntryModules()")


def test_set_view_overview_calls_wait_for_business_list():
    set_view = _function_source("setView")
    for call in ("window.obysDataOverview?.openGlance()", "window.obysDataOverview?.openDelivery()"):
        for line in [ln for ln in set_view.splitlines() if call in ln]:
            assert "ownerBusinessApisAllowed()" in line, line


def test_persist_settings_refetches_business_list_when_not_ready():
    body = _function_source("persistSettingsToServer")
    assert 'businessListStatus.state !== "ready"' in body
    assert body.index('method: "PUT"') < body.index("loadServerSettings()")


@needs_node
def test_failed_business_list_keeps_retry_visible_with_local_businesses(tmp_path):
    harness = "\n".join([
        "const out = {};",
        "const els = { businessListState: { innerHTML: '', classList: { hidden: true, toggle(_, v) { this.hidden = v; } } } };",
        "const state = { settings: { businesses: [{ id: 'biz-local', name: '새 사업자' }] } };",
        "const businessListStatus = { state: 'failed', message: '등록된 사업자가 없습니다' };",
        "function ownerApisAllowed() { return true; }",
        "function retryBusinessList() {}",
        "function renderState(el, spec) { el.innerHTML = spec.title + '|' + spec.actions.map(a => a.label).join(','); }",
        _function_source("renderBusinessListState"),
        "renderBusinessListState();",
        "out.failed = { hidden: els.businessListState.classList.hidden, html: els.businessListState.innerHTML };",
        "businessListStatus.state = 'loading';",
        "renderBusinessListState();",
        "out.loading = { hidden: els.businessListState.classList.hidden };",
        "console.log(JSON.stringify(out));",
    ])
    result = _run_node(harness, tmp_path)
    assert result["failed"]["hidden"] is False
    assert "다시 시도" in result["failed"]["html"]
    assert result["loading"]["hidden"] is True
