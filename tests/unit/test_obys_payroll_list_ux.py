"""오비서 급여 현황 개편 1단계(B안) — 월 이동·상태 탭·명세 없음 행·용어 정리 검증.

오른쪽 상세 패널은 CEO 결정(2026-11-11, B안)으로 이번 범위에서 뺐다. 상세는 기존처럼 목록 아래, 편집기는 화면 아래에 둔다.

admin.html 의 실제 renderPeopleV41(급여 분기)·payrollListHtmlV41 등을 node 로 실행한다(의존 객체만 대역).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys" / "admin.html"
SRC = HTML.read_text(encoding="utf-8")

PREFIXES = (
    "function escapeHtml(", "function metricHtml(", "function payrollMoneyV41(", "function payrollDisplayV41(",
    "function attendanceStatusV41(", "function attendanceReviewV41(", "function attendanceMinutesV41(",
    "function displayDateV41(", "function attendanceTimeV41(", "function attendanceDisplayV41(", "function liveRow(",
    "function filteredRows(c)", "function employmentMatchV41(", "function peopleWorkLinksV41(",
    "function peopleMonthBoundsV41(", "function activeMonthV41(", "async function peopleLoadV41(",
    "function payrollLatestMonthV41(", "function payrollMonthRowsV41(", "function payrollDetailRowV41(",
    "function payrollPayloadV41(", "function payrollNumV41(", "function payrollDeductionTotalV41(",
    "function payrollContractIdV41(", "function payrollUnsplitV41(", "function payrollPaymentCheckV41(",
    "function payrollFlagsV41(", "function payrollSummaryV41(", "function payrollSummaryHtmlV41(",
    "function payrollBasisHtmlV41(", "function payrollRowNoteV41(", "function payrollLinkCacheV41(",
    "function payrollPayoutV41(", "function payrollPayoutBadgeV41(", "function payrollPayoutOpenCountV41(",
    "function payrollPayoutPanelV41(", "function payrollStatusKeyV41(", "function payrollDashV41(",
    "function payrollWorkTextV41(", "function payrollActionV41(", "function payrollListHtmlV41(",
    "function payrollFocusActionV41(", "function payrollSetEmployeeV41(", "async function payrollSelectRowV41(",
    "function payrollLinksV41(", "async function payrollResolveV41(", "function payrollApprovedV41(",
    "function payrollEditorSelectionV41(",
)


def _lines(prefixes):
    out = []
    for prefix in prefixes:
        out.append(next(line.strip() for line in SRC.splitlines() if line.strip().startswith(prefix)))
    return "\n".join(out)


PRELUDE = r"""const assert=require('node:assert/strict');
const state={route:'payroll',peopleMonth:'2026-11',payrollMonth:'2026-11',payrollMonthSelected:false,readRole:'owner',status:'전체 상태',selected:{},search:'',businessId:'b1',businessName:'가게'};
const configs={payroll:{title:'급여 현황',columns:['직원','지점','총지급액','공제','실수령','상태'],hierarchy:[]},attendance:{title:'근태 현황',columns:['직원','일자','지점','근무시간','상태'],hierarchy:[]}};
const elements={};const document={getElementById:id=>elements[id]||(elements[id]={addEventListener(){},dataset:{}}),querySelector:()=>null};
let mounts=[];const window={ObysPayrollAdmin:{mount(host,opt){mounts.push(opt);return Promise.resolve()}}};
let buttons=[];
const page={innerHTML:'',className:'',insertAdjacentHTML(pos,html){page.innerHTML+=html},querySelectorAll(sel){
  if(sel==='[data-person]'){buttons=[...page.innerHTML.matchAll(/<button[^>]*data-person="([^"]*)"[^>]*>/g)].map(m=>{const a=/data-action="([^"]*)"/.exec(m[0]);return {dataset:{person:m[1],action:a?a[1]:undefined},onclick:null}});return buttons}
  if(sel==='[data-payroll-tab]'){return [...page.innerHTML.matchAll(/data-payroll-tab="([^"]*)"/g)].map(m=>({dataset:{payrollTab:m[1]},onclick:null}))}
  return []}};
const kstDateV41=()=>'2026-11-11';
function bindWorkSchedulesV41(){};function loadLiveRoute(){loads++};function openDetailV41(){};function showToast(){};
let loads=0;
function renderPageV41(){renderPeopleV41(configs.payroll)}
const financeApiV41=async()=>({employees:[]});const BIZDOC_API_V41='/x';
"""

TAIL = r"""
const mk=(id,raw,name,extra={})=>liveRow({id,raw:{payroll_month:'2026-11',...raw},display:{직원:name},...extra},configs.payroll);
configs.payroll.data=[
 mk('p1',{gross_pay:3000000,total_deductions:300000,net_pay:2700000,status:'draft',statement_payload:{work_summary:{work_days:20,work_minutes:9630}}},'초안직원'),
 mk('p2',{gross_pay:2000000,total_deductions:200000,net_pay:1800000,status:'confirmed'},'확정직원'),
 mk('p3',{gross_pay:1000000,total_deductions:100000,net_pay:900000,status:'paid'},'지급직원'),
];
state.peopleEmployees={b1:[{id:'e9',name:'명세없는직원',email:'none@x.test',branch:'본점'},{id:'e8',name:'퇴사직원',email:'out@x.test',status:'retired'}]};
"""


def _run(body: str, tmp_path: Path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node 미설치")
    renderer = "function renderPeopleV41(c){" + SRC.split("function renderPeopleV41(c){", 1)[1].split("function renderPageV41(){", 1)[0]
    script = tmp_path / "payroll-list-ux.cjs"
    script.write_text(PRELUDE + _lines(PREFIXES) + "\n" + renderer + TAIL + "(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exit(1)});\n", encoding="utf-8")
    done = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_month_prev_next_buttons_use_the_month_input_path_and_request_previous_month(tmp_path):
    _run(r"""
const calls=[];
async function workspaceApi(path){calls.push(path);return {records:[],has_more:false}};
globalThis.workspaceApi=workspaceApi;
renderPageV41=()=>{};
renderPeopleV41(configs.payroll);
assert(page.innerHTML.includes('id="payrollMonthPrev"')&&page.innerHTML.includes('id="payrollMonthNext"'));
assert(page.innerHTML.indexOf('id="payrollMonthPrev"')<page.innerHTML.indexOf('id="peopleMonth"'));
assert(page.innerHTML.indexOf('id="peopleMonth"')<page.innerHTML.indexOf('id="payrollMonthNext"'));
assert(page.innerHTML.indexOf('class="toolbar payroll-toolbar"')<page.innerHTML.indexOf('id="payrollMonthPrev"'));
elements.payrollMonthPrev.onclick();
assert.equal(state.payrollMonth,'2026-10');assert.equal(state.payrollMonthSelected,true);assert.equal(loads,1);
await peopleLoadV41();
assert(calls[0].includes('date_from=2026-10-01'),calls[0]);assert(calls[0].includes('date_to=2026-10-31'),calls[0]);
state.payrollMonth='2026-12';renderPeopleV41(configs.payroll);elements.payrollMonthNext.onclick();assert.equal(state.payrollMonth,'2027-01');
state.payrollMonth='2026-01';renderPeopleV41(configs.payroll);elements.payrollMonthPrev.onclick();assert.equal(state.payrollMonth,'2025-12');
""", tmp_path)


def test_status_tab_counts_match_data_and_selecting_a_tab_filters_rows(tmp_path):
    _run(r"""
renderPeopleV41(configs.payroll);
const h=page.innerHTML;
for(const [k,n] of [['none',1],['draft',1],['confirmed',1],['paid',1],['all',4]]){
  assert(new RegExp('data-payroll-tab="'+k+'"[^>]*>[^<]*'+n+'<').test(h),k);
}
assert(h.includes('명세 없음 1')&&h.includes('초안 1')&&h.includes('확정 1')&&h.includes('지급 완료 1')&&h.includes('전체 4'));
for(const [tab,keep,drop] of [['draft','초안직원','확정직원'],['confirmed','확정직원','지급직원'],['paid','지급직원','초안직원'],['none','명세없는직원','초안직원']]){
  state.payrollTab=tab;renderPeopleV41(configs.payroll);
  const body=page.innerHTML.split('id="payrollTabs"')[1];
  assert(body.includes(keep),tab+' keep');assert(!body.split('</nav>')[1].includes(drop),tab+' drop');
}
state.payrollTab='all';renderPeopleV41(configs.payroll);
for(const n of ['초안직원','확정직원','지급직원','명세없는직원'])assert(page.innerHTML.includes(n),n);
assert(!page.innerHTML.includes('퇴사직원'));
""", tmp_path)


def test_approved_employee_without_statement_gets_a_no_statement_row_with_make_button(tmp_path):
    _run(r"""
state.payrollTab='none';renderPeopleV41(configs.payroll);
const h=page.innerHTML;
assert(h.includes('명세없는직원')&&h.includes('data-status-badge="none"')&&h.includes('명세 없음</span>'));
assert(h.includes('data-action="draft">명세 만들기</button>'));
// 명세가 있는 직원은 '명세 없음'에 중복되지 않는다.
configs.payroll.data.push(mk('p9',{gross_pay:1,net_pay:1,status:'draft'},'명세없는직원',{employee_key:'employee:b1:none@x.test'}));
renderPeopleV41(configs.payroll);assert(!page.innerHTML.includes('data-status-badge="none"'));
// 직원 목록 조회 실패 안내
configs.payroll.data.pop();state.peopleEmployees={};state.peopleEmployeesFailed='b1';renderPeopleV41(configs.payroll);
assert(page.innerHTML.includes('직원 목록을 불러오지 못했습니다 · ')&&page.innerHTML.includes('id="payrollEmployeesRetry"'));
elements.payrollEmployeesRetry.onclick();assert.equal(state.peopleEmployeesFailed,'');
""", tmp_path)


def test_list_columns_work_time_and_next_action_buttons(tmp_path):
    _run(r"""
renderPeopleV41(configs.payroll);const h=page.innerHTML;
for(const t of ['<th>직원</th>','근무시간','총지급','<th class="c-net num">실수령</th>','<th class="center">상태</th>','다음 할 일'])assert(h.includes(t),t);
// 디자인 정렬: 금액·시간은 오른쪽(num), 상태·버튼은 가운데(center), 이름은 왼쪽
for(const t of ['<td class="c-work num">','<td class="c-gross num">','<td class="c-net num">','<td class="c-state center">','<td class="c-act center">','<td class="c-name">'])assert(h.includes(t),t);
assert(h.includes('160시간 30분'));   // 9630분
assert(h.includes('data-action="confirm">검토·확정</button>'));
assert(h.includes('data-action="pay">지급 처리</button>'));
assert(h.split('data-action="view">명세서 보기</button>').length===2);
assert.equal(payrollWorkTextV41({__live:{raw:{}}}),'-');
assert.equal(payrollWorkTextV41({__live:{raw:{statement_payload:{work_summary:{work_minutes:125}}}}}),'2시간 5분');
""", tmp_path)


def test_summary_has_pending_payment_amount_for_confirmed_unpaid_only(tmp_path):
    _run(r"""
renderPeopleV41(configs.payroll);const h=page.innerHTML;
assert(h.includes('지급 대기 금액'));
const card=h.split('지급 대기 금액')[1].split('</article>')[0];
assert(card.includes('1,800,000원'),card);assert(!card.includes('900,000원'));
for(const l of ['총지급 합계','공제 합계','실수령 합계','확정 대기'])assert(h.includes(l),l);
""", tmp_path)


def test_row_click_shows_detail_below_list_and_editor_stays_at_bottom(tmp_path):
    _run(r"""
renderPeopleV41(configs.payroll);
assert(!page.innerHTML.includes('id="payrollPanel"')&&!page.innerHTML.includes('급여명세 상세'));
assert(page.innerHTML.includes('id="payrollEditor"'),'관리자 편집기는 항상 화면 아래에 있다');
buttons.find(x=>x.dataset.person==='record:p2'&&!x.dataset.action).onclick();
assert.equal(state.payrollDetailKey,'record:p2');
let h=page.innerHTML;
assert(h.indexOf('id="payrollTabs"')<h.indexOf('급여명세 상세'),'상세는 목록 아래');
assert(h.indexOf('급여명세 상세')<h.indexOf('id="payrollEditor"'),'편집기는 상세 아래');
assert(!h.includes('id="payrollPanel"')&&!String(page.className).includes('panel-open'));
assert.equal(mounts.at(-1).month,'2026-11');
// 이름을 다시 누르면 상세를 접는다.
buttons.find(x=>x.dataset.person==='record:p2'&&!x.dataset.action).onclick();
assert.equal(state.payrollDetailKey,'');assert(!page.innerHTML.includes('급여명세 상세'));
// 다음 할 일 버튼은 같은 직원이어도 접지 않고 해당 동작을 가리킨다.
buttons.find(x=>x.dataset.person==='record:p2'&&x.dataset.action==='pay').onclick();
assert.equal(state.payrollDetailKey,'record:p2');assert.equal(state.payrollPanelAction,'pay');
buttons.find(x=>x.dataset.person==='record:p2'&&x.dataset.action==='pay').onclick();
assert.equal(state.payrollDetailKey,'record:p2');
// 명세 없음 행: 승인된 직원 id 로 아래 편집기를 선택한다. '명세 없음' 상세 칸은 만들지 않는다.
state.payrollTab='none';renderPeopleV41(configs.payroll);
buttons.find(x=>x.dataset.action==='draft').onclick();
assert.equal(state.payrollEmployeeId,'e9');assert.equal(state.payrollRequireSelection,false);
assert(page.innerHTML.includes('id="payrollEditor"')&&!page.innerHTML.includes('명세 없음 ·'));
assert.equal(mounts.at(-1).selectedEmployeeId,'e9');
""", tmp_path)


def test_member_role_sees_no_edit_buttons_or_editor(tmp_path):
    _run(r"""
state.readRole='member';renderPeopleV41(configs.payroll);
for(const t of ['명세 만들기','검토·확정','지급 처리','새 급여명세','id="payrollEditor"'])assert(!page.innerHTML.includes(t),t);
assert(page.innerHTML.includes('명세서 보기'));
state.payrollDetailKey='record:p1';renderPeopleV41(configs.payroll);
assert(page.innerHTML.includes('급여명세 상세')&&!page.innerHTML.includes('id="payrollEditor"'));assert.equal(mounts.length,0);
""", tmp_path)


def test_no_internal_wording_in_payroll_screen_except_error(tmp_path):
    _run(r"""
configs.payroll.data.push(mk('p7',{status:'draft'},'값없음직원'));
state.liveSource={name:'acct_legacy'};
renderPeopleV41(configs.payroll);
assert(!page.innerHTML.includes('미집계'),'미집계');assert(!page.innerHTML.includes('등록 원장'),'원장');assert(!page.innerHTML.includes('기존 자료'));
assert(/class="source-status" hidden/.test(page.innerHTML));
state.payrollDetailKey='record:p7';renderPeopleV41(configs.payroll);assert(!page.innerHTML.includes('미집계'));
state.payrollDetailKey='';state.liveError='서버 오류(500)';renderPeopleV41(configs.payroll);
assert(page.innerHTML.includes('서버 오류(500)')&&page.innerHTML.includes('id="peopleRetry"')&&!/class="source-status" hidden/.test(page.innerHTML));
// 근태 화면의 출처 문구는 그대로
state.liveError='';state.readRole='member';state.route='attendance';configs.attendance.data=[];renderPeopleV41(configs.attendance);
assert(page.innerHTML.includes('등록 원장'));
""", tmp_path)


def test_b_scope_has_no_right_panel_or_panel_only_handlers():
    """B안: 오른쪽 패널·패널 닫기·Esc 처리·렌더 중 상세 자동 정리를 넣지 않는다(반려 5회의 원인 영역)."""
    for token in ("payroll-panel", "panel-open", "payrollPanelClose", "payrollScrim", "visibleKeys", 'id="payrollPanel"'):
        assert token not in SRC, token
    assert not any(l.strip().startswith('document.addEventListener("keydown"') and "payrollDetailKey" in l for l in SRC.splitlines())
    assert "payrollDetailRowV41(inMonth,state.payrollDetailKey,identity)" in SRC


def test_duplicate_payroll_month_input_removed_from_filters_but_attendance_unchanged():
    body = next(l for l in SRC.splitlines() if l.strip().startswith("function peopleFiltersV41("))
    assert 'data-people-filter="peopleMonth"' not in body
    assert 'data-people-filter="peopleEmployee"' in body and 'data-people-filter="peopleBranch"' in body
    assert SRC.count('id="peopleMonth"') == 1


def test_css_mobile_card_and_touch_targets():
    assert re.search(r"@media\(max-width:600px\)\{\.table-wrap\.payroll-list table\{min-width:0", SRC)
    assert re.search(r"\.payroll-toolbar \.btn,[^{]*\.payroll-list \.btn[^{]*\{min-height:44px\}", SRC)


def test_review_rework_guards_present():
    """재작업 지적 1~3: 명세 없음 직원 필터·편집기 null 가드·지점 선택지."""
    html = SRC
    assert "const haveKeys=new Set(payrollMonthRowsV41(all,month).map(identity))" in html
    assert "noneEmpOk" in html
    assert "page.insertAdjacentHTML(\"beforeend\",'<section class=\"section\" id=\"payrollEditor\"></section>')" in html
    assert "const editorEl=document.getElementById(\"payrollEditor\")" in html
    assert "if(!editorEl);else if(sel.linking)editorEl.innerHTML=" in html
    assert "window.ObysPayrollAdmin?window.ObysPayrollAdmin.mount(editorEl" in html
    assert ".catch(error=>{const el=document.getElementById(\"payrollEditor\");if(el)el.textContent=" in html
    assert "state.payrollNoneAll" in html


# ---- R3: 실제 실행 시나리오(node 하네스) — 소스 문자열이 아니라 renderPeopleV41 을 돌려 증명한다 ----

SCEN = r"""
const rows=configs.payroll.data=[
 mk('pA',{gross_pay:1,net_pay:1,status:'draft',branch:'본점'},'AAA직원',{employee_key:'employee:b1:a@x.test'}),
 mk('pB',{gross_pay:2,net_pay:2,status:'draft',branch:'강남점'},'BBB직원',{employee_key:'employee:b1:b@x.test'}),
];
state.peopleEmployees={b1:[{id:'eC',name:'CCC직원',email:'c@x.test',branch:'제주점'}]};
const tableOf=()=>page.innerHTML.split('id="payrollTabs"')[1].split('</nav>')[1].split('</section>')[0];
"""


def test_scenario_search_does_not_turn_other_statement_employees_into_no_statement(tmp_path):
    _run(SCEN + r"""
state.search='AAA';renderPeopleV41(configs.payroll);
const t=tableOf();
assert(t.includes('AAA직원'),'A 보임');
assert(!t.includes('BBB직원'),'B 는 검색에 걸러져 목록에 없다(명세 없음 행으로도 안 나온다)');
assert(!/data-status-badge="none"[^]*BBB/.test(t));
""", tmp_path)


def test_scenario_branch_with_only_no_statement_employee_is_in_branch_options(tmp_path):
    _run(SCEN + r"""
renderPeopleV41(configs.payroll);
const sel=page.innerHTML.split('id="payrollBranch"')[1].split('</select>')[0];
assert(sel.includes('value="제주점"'),'명세 없음 직원 C 만 있는 지점 선택지: '+sel);
""", tmp_path)


def test_scenario_detail_hidden_when_search_filters_target_out_and_never_says_no_statement(tmp_path):
    _run(SCEN + r"""
renderPeopleV41(configs.payroll);
buttons.find(x=>x.dataset.person==='employee:b1:a@x.test'&&!x.dataset.action).onclick();
assert.equal(state.payrollDetailKey,'employee:b1:a@x.test');
assert(page.innerHTML.includes('AAA직원')&&page.innerHTML.includes('급여명세 상세'));
// 검색 BBB 로 A 가 목록에서 빠지면 A 상세도 보이지 않고, A 를 '명세 없음'으로 표시하지 않는다.
state.search='BBB';renderPeopleV41(configs.payroll);
assert(!page.innerHTML.includes('급여명세 상세'));assert(!page.innerHTML.includes('명세 없음 ·'));
assert(!/data-status-badge="none"[^]*AAA/.test(page.innerHTML));
// 검색을 풀면 그대로 다시 보인다.
state.search='';renderPeopleV41(configs.payroll);
assert(page.innerHTML.includes('급여명세 상세'));
""", tmp_path)


def test_scenario_branch_filter_never_flips_statement_employee_to_no_statement(tmp_path):
    """지점 필터로 가려진 명세 직원이 '명세 없음' 행·상세로 뒤집히면 안 된다."""
    _run(SCEN + r"""
state.payrollDetailKey='employee:b1:a@x.test';state.peopleBranch='본점';renderPeopleV41(configs.payroll);
assert(page.innerHTML.includes('급여명세 상세')&&!page.innerHTML.includes('명세 없음 ·'));
state.peopleBranch='강남점';renderPeopleV41(configs.payroll);
assert(!page.innerHTML.includes('급여명세 상세')&&!page.innerHTML.includes('명세 없음 ·'));
assert(!/data-status-badge="none"[^]*AAA/.test(page.innerHTML));
""", tmp_path)


def test_scenario_loading_render_without_editor_does_not_throw_even_when_linking(tmp_path):
    _run(SCEN + r"""
const real=document.getElementById;
document.getElementById=id=>id==='payrollEditor'?null:real(id);   // liveLoading 렌더: 패널·편집기 없음
state.payrollDetailKey='employee:b1:a@x.test';state.payrollLinking=true;state.liveLoading=true;
const before=mounts.length;
renderPeopleV41(configs.payroll);                                  // sel.linking 분기
state.payrollLinking=false;renderPeopleV41(configs.payroll);       // mount 분기
assert.equal(mounts.length,before,'편집기가 없으면 mount 하지 않는다');
""", tmp_path)


def test_scenario_on_saved_loading_render_never_mounts_null(tmp_path):
    _run(SCEN + r"""
const hosts=[],opts=[];window.ObysPayrollAdmin={mount(host,opt){hosts.push(host);opts.push(opt);if(host===null)throw new TypeError('mount(null)');return Promise.resolve()}};
state.payrollDetailKey='employee:b1:a@x.test';state.payrollLinking=false;
renderPeopleV41(configs.payroll);
assert.equal(hosts.length,1);
const onSaved=opts.at(-1).onSaved;
const real=document.getElementById;
loadLiveRoute=function(){state.liveLoading=true;document.getElementById=id=>id==='payrollEditor'?null:real(id);renderPeopleV41(configs.payroll)};
onSaved();
assert.equal(hosts.length,1);assert(hosts.every(h=>h!==null));
""", tmp_path)


def test_scenario_mount_rejection_is_caught_and_shown_in_editor(tmp_path):
    _run(SCEN + r"""
const el={textContent:''};const real=document.getElementById;
document.getElementById=id=>id==='payrollEditor'?el:real(id);
window.ObysPayrollAdmin={mount(){return Promise.reject(new Error('실패사유'))}};
process.on('unhandledRejection',e=>{console.error('unhandled',e);process.exit(1)});
state.payrollDetailKey='employee:b1:a@x.test';renderPeopleV41(configs.payroll);
await new Promise(r=>setTimeout(r,20));
assert(el.textContent.includes('실패사유'),el.textContent);
""", tmp_path)


def test_scenario_branch_and_month_change_close_detail_like_before(tmp_path):
    """B안: 지점·급여월을 바꾸면 기존 화면처럼 상세를 접는다(렌더 중 자동 정리는 하지 않는다)."""
    _run(SCEN + r"""
const listeners={};
document.getElementById=id=>listeners[id]||(listeners[id]={dataset:{},addEventListener(ev,fn){this['on_'+ev]=fn}});
const A='employee:b1:a@x.test';
renderPeopleV41(configs.payroll);
state.payrollDetailKey=A;document.getElementById('payrollBranch').on_change({target:{value:'본점'}});
assert.equal(state.peopleBranch,'본점');assert.equal(state.payrollDetailKey,'');
state.peopleBranch='';state.payrollDetailKey=A;const n=loads;
document.getElementById('peopleMonth').onchange({target:{value:'2026-10'}});
assert.equal(state.payrollMonth,'2026-10');assert.equal(state.payrollDetailKey,'');assert.equal(loads,n+1);
// 이전 달·다음 달 버튼도 같은 경로를 탄다.
state.payrollMonth='2026-11';renderPeopleV41(configs.payroll);state.payrollDetailKey=A;
document.getElementById('payrollMonthPrev').onclick();
assert.equal(state.payrollMonth,'2026-10');assert.equal(state.payrollDetailKey,'');
""", tmp_path)


