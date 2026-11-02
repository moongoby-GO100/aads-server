"""계약서 기준 일정: 선택란 자동 반영 + 부족 항목 표시·보완 후 저장 등록 검증."""
import json
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.api import obys_workspaces as api

ADMIN = Path("app/static/apps/obys/admin.html")
FREE_DAILY = "해당 없음(용역계약). 회차별 수행 일시는 상호 협의로 정한다."
FREE_DAYS = "일정은 상호 협의(회차별 수락·거절 가능)"
FREE_REST = "1회 1.5시간, 수행자가 자유롭게 이용"


def _free(clock):
    return {"daily_work_schedule": FREE_DAILY, "work_days": FREE_DAYS, "rest_time": FREE_REST,
            "work_time": f"회차별 상호 협의. 1회 기준 {clock} 중 휴게 1.5시간 제외 10.5시간"}


REAL = {
    "홍석빈": {"daily_work_schedule": "\n".join(f"{d} 09:00-21:00 (휴게 90분, 실근로 10시간 30분)" for d in "월화수목일"),
              "work_days": "주 5일(월·화·수·목·일)", "work_time": "09:00-21:00", "rest_time": "90분",
              "start_date": "2026-01-01", "end_date": "2026-12-31"},
    "김민우": _free("09:00~21:00"),
    "하영훈": _free("22:00~익일 10:00"),
    "양재혁": _free("21:00~익일 09:00"),
}
EMAILS = {"홍석빈": "hong@x.test", "김민우": "kim@x.test", "하영훈": "ha@x.test", "양재혁": "yang@x.test"}


def _result(name, terms=None):
    terms = REAL[name] if terms is None else terms
    pattern = api.contract_schedule_pattern(terms)
    suggested, missing, filled = api.contract_schedule_suggestion(terms, pattern)
    return {"employee_email": EMAILS.get(name, name), "employee_name": name, "contract_type": "regular",
            "branch": "", "pattern": pattern, "basis": "\n".join(str(terms.get(k) or "") for k in (
                "daily_work_schedule", "work_days", "work_time", "rest_time")), "reason": "",
            "suggested": suggested, "missing": missing, "filled_from_contract": filled,
            "contract_period": api._contract_period(terms)}


# ------------------------------------------------------------------ 서버
def test_regular_contract_is_complete():
    result = _result("홍석빈")
    assert result["missing"] == []
    assert all(result["filled_from_contract"].values())
    assert result["suggested"]["workdays"] == [0, 1, 2, 3, 6]
    expected = {"start_time": "09:00", "end_time": "21:00", "break_minutes": 90, "overnight": False}
    assert result["suggested"]["default"] == expected
    assert result["suggested"]["weekdays"] == {d: expected for d in (0, 1, 2, 3, 6)}
    assert result["pattern"] == result["suggested"]["weekdays"]


@pytest.mark.parametrize("name,start,end,overnight", [
    ("김민우", "09:00", "21:00", False), ("하영훈", "22:00", "10:00", True), ("양재혁", "21:00", "09:00", True)])
def test_freelancer_keeps_time_and_break_but_never_invents_workdays(name, start, end, overnight):
    terms = REAL[name]
    assert api.contract_schedule_pattern(terms) == {}  # 기존 동작 유지
    result = _result(name)
    assert result["pattern"] == {}
    assert result["suggested"]["workdays"] == [] and result["suggested"]["weekdays"] == {}
    assert result["suggested"]["default"] == {"start_time": start, "end_time": end, "break_minutes": 90, "overnight": overnight}
    assert result["missing"] == ["근무요일", "계약기간"]
    assert result["filled_from_contract"] == {"근무요일": False, "출근시각": True, "퇴근시각": True, "휴게시간": True, "계약기간": False}


def test_missing_lists_every_unspecified_field():
    suggested, missing, filled = api.contract_schedule_suggestion({})
    assert missing == ["근무요일", "출근시각", "퇴근시각", "휴게시간", "계약기간"]
    assert not any(filled.values())
    assert suggested == {"workdays": [], "weekdays": {}, "default": {
        "start_time": None, "end_time": None, "overnight": False, "break_minutes": None}}


def test_weekday_count_without_list_is_not_a_workday_list():
    terms = {"work_days": "주 5일", "work_time": "09:00-18:00", "rest_time": "60분"}
    suggested, missing, _ = api.contract_schedule_suggestion(terms)
    assert suggested["workdays"] == [] and suggested["weekdays"] == {}
    assert "근무요일" in missing and "출근시각" not in missing and "휴게시간" not in missing
    assert suggested["default"]["start_time"] == "09:00" and suggested["default"]["break_minutes"] == 60


def test_explicit_workdays_with_time_but_no_break():
    terms = {"work_days": "월·수", "work_time": "10:00-19:00"}
    suggested, missing, _ = api.contract_schedule_suggestion(terms)
    assert suggested["workdays"] == [0, 2] and sorted(suggested["weekdays"]) == [0, 2]
    assert missing == ["휴게시간", "계약기간"]


@pytest.mark.parametrize("text,expected", [
    ("1회 1.5시간, 수행자가 자유롭게 이용", 90), ("90분", 90), ("1시간 30분", 90), ("1시간", 60),
    ("30분 이상", 30), ("자유 이용", None)])
def test_break_text_parsing(text, expected):
    assert api._contract_break_minutes(text) == expected


def test_work_time_break_needs_keyword_so_worked_hours_are_not_a_break():
    assert api._contract_break_minutes("1회 기준 09:00~21:00 중 10.5시간", need_keyword=True) is None
    assert api._contract_break_minutes("1회 기준 09:00~21:00 중 휴게 1.5시간 제외 10.5시간", need_keyword=True) == 90


def test_different_times_per_weekday():
    terms = {"daily_work_schedule": "월 09:00-18:00 (휴게 60분)\n화 10:00-20:00 (휴게 30분)"}
    suggested, missing, _ = api.contract_schedule_suggestion(terms)
    assert suggested["weekdays"][0]["end_time"] == "18:00" and suggested["weekdays"][1]["break_minutes"] == 30
    assert suggested["workdays"] == [0, 1] and missing == ["계약기간"]


class Connection:
    def __init__(self, terms):
        self.terms = terms

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, sql, *args):
        if "yeoljeong_contracts" in sql:
            return [dict(employee_email="kim@x.test", employee_name="김민우", contract_type="freelance",
                         signed_at="2026-10-01", contract_payload=self.terms)]
        return []

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_preview_response_is_backward_compatible_and_adds_form_keys(monkeypatch):
    monkeypatch.setattr(api, "_require_attendance_admin", lambda u: None)
    monkeypatch.setattr(api, "_schedule_business", AsyncMock())
    monkeypatch.setattr(api.upload_svc, "_tenant", lambda u: "tenant-a")
    monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=Connection(REAL["김민우"])))
    result = await api._contract_schedules("biz-a", api.ContractSchedulesIn(date_from="2026-11-01", date_to="2026-11-30"), {})
    employee = result["employees"][0]
    for key in ("employee_email", "employee_name", "contract_type", "pattern", "basis", "created", "updated",
                "skipped", "unparseable", "schedules", "reason"):
        assert key in employee, key
    assert employee["unparseable"] == 1 and employee["created"] == 0 and "직접 등록" in employee["reason"]
    assert employee["missing"] == ["근무요일", "계약기간"]
    assert employee["suggested"]["default"]["start_time"] == "09:00"
    assert employee["filled_from_contract"]["휴게시간"] is True
    assert employee["contract_period"] == {"start_date": None, "end_date": None}
    json.dumps(employee)


# ------------------------------------------------------------------ 정적
def test_static_form_markup_and_sizes():
    html = ADMIN.read_text()
    for text in ("계약서에 없음 — 입력 필요", "계약서 기준", "기존 일정 보존", "덮어쓰기", "저장·등록", "모두 저장·등록",
                 "서명된 계약서는 바뀌지 않습니다. 보완한 값은 근무표에만 저장됩니다.", "계약서 기준 원문",
                 ".cf-missing{", "@media(max-width:600px){.cf-card .cf-row"):
        assert text in html, text
    assert ".cf-card input,.cf-card select,.cf-card summary,.cf-card button,.cf-top button{min-height:44px" in html
    assert "적용 (기존 일정 보존)" not in html  # 읽기 전용 표 + 적용 버튼 제거


# ------------------------------------------------------------------ node
# 아래 JS 의 \x22 는 큰따옴표, \x3c 는 꺾쇠 여는 기호다(원격 셸 래퍼가 해당 문자열 조합을 못 읽어서 이스케이프).
HARNESS = r"""
const assert=require('node:assert/strict');
const state={businessId:'b1',peopleMonth:'2026-11',workSchedules:{month:{rows:[]}}};
const kstDateV41=()=>'2026-11-02';
const HONG='hong@x.test',HA='ha@x.test',YANG='yang@x.test';
const toasts=[];function showToast(m){toasts.push(m)}
let reloads=0;async function loadLiveRoute(){reloads++}
const calls=[];let apiResult={employees:EMPLOYEES};
async function workspaceApi(path,opt){calls.push([path,opt]);return path.includes('from-contracts')?apiResult:{}}
const flush=async()=>{for(let i=0;i<6;i++)await new Promise(r=>setImmediate(r))};
function parseAttrs(s){const a={};for(const m of s.matchAll(/([\w-]+)=\x22([^\x22]*)\x22/g))a[m[1]]=m[2];return a}
function makeCard(html){
  const at=parseAttrs(/\x3csection class=\x22cf-card\x22([^>]*)>/.exec(html)[1]);
  const card={html,dataset:{cfEmail:at['data-cf-email'],cfName:at['data-cf-name'],cfBranch:at['data-cf-branch']},controls:[],status:{textContent:''}};
  for(const m of html.matchAll(/\x3c(input|select)\b([^>]*)>/g)){const a=parseAttrs(m[2]),raw=m[2].replace(/\x22[^\x22]*\x22/g,'');
    card.controls.push({tag:m[1],name:a.name,type:a.type,value:a.value??(m[1]==='select'?'work':''),checked:/\bchecked\b/.test(raw),classList:{remove(){}},closest:()=>card})}
  card.ctl=(name,value)=>card.controls.find(c=>c.name===name&&(value===undefined||c.value===String(value)));
  card.checkedDays=()=>[0,1,2,3,4,5,6].filter(i=>card.ctl('cf_weekday',i).checked);
  card.querySelector=sel=>{const p=sel.split('\x22');
    if(p[0]==='[name='&&p.length===3)return card.ctl(p[1])||null;
    if(p[0]==='input[name='&&p.length===5)return card.ctl(p[1],p[3])||null;
    if(sel==='input[type=\x22radio\x22]:checked')return card.controls.find(c=>c.type==='radio'&&c.checked)||null;
    if(sel==='[data-cf-status]')return card.status;
    throw new Error('unsupported '+sel)};
  return card}
function makeTarget(){return {isConnected:true,textContent:'',cards:[],buttons:[],handlers:{},_html:'',
  get innerHTML(){return this._html},
  set innerHTML(h){this._html=h;this.cards=[...h.matchAll(/\x3csection class=\x22cf-card\x22[\s\S]*?\x3c\/section>/g)].map(m=>makeCard(m[0]));
    this.buttons=this.cards.map(c=>(c.saveButton={disabled:false,closest:()=>c,onclick:null}));
    this.all={disabled:false,onclick:null};this.buttons.push(this.all);this.allStatus={textContent:''}},
  addEventListener(n,f){this.handlers[n]=f},
  querySelectorAll(sel){if(sel==='[data-cf-save]')return this.buttons.filter(b=>b!==this.all);if(sel==='[data-cf]')return this.cards;if(sel==='button')return this.buttons;throw new Error(sel)},
  querySelector(sel){if(sel==='[data-cf-save-all]')return this.all;if(sel==='[data-cf-all-status]')return this.allStatus;throw new Error(sel)}}}
const cl=()=>({set:{},toggle(n,on){this.set[n]=!!on}});
function makeForm(checkedEmails){return {elements:{date_from:{value:'2026-11-01'},date_to:{value:'2026-11-30'},start_time:{value:'09:00',classList:cl()},end_time:{value:'18:00',classList:cl()},break_minutes:{value:'60',classList:cl()}},
  weekdays:[0,1,2,3,4,5,6].map(i=>({value:String(i),checked:i<5})),events:0,
  querySelectorAll(sel){if(sel==='input[name=\x22weekday\x22]')return this.weekdays;if(sel==='input[name=\x22employee\x22]:checked')return checkedEmails.map(v=>({value:v}));return []},
  dispatchEvent(){this.events++}}}
const els={};let form=null;
const document={getElementById:id=>id==='scheduleBulk'?form:(els[id]=els[id]||makeTarget())};
const countOf=(h,t)=>h.split(t).length-1;
const NOT_IN='계약서에 없음 — 입력 필요';
const click=async b=>{b.onclick();await flush()};
const posts=()=>calls.filter(c=>c[0].endsWith('/attendance/schedules/bulk'));
async function show(email){await previewContractSchedulesV41(email);return els.contractPreviewResult}
"""


def _need_node():
    if not shutil.which("node"):
        pytest.skip("node 없음")


def _functions():
    html = ADMIN.read_text()

    def one(prefix):
        return next(line.strip() for line in html.splitlines() if line.strip().startswith(prefix))

    start = html.index("    function contractSuggestV41(")
    end = html.index("    function openRosterEditorV41(")
    return "\n".join([one("function escapeHtml("), one("function peopleMonthBoundsV41("), html[start:end]])


def _run(tmp_path, body, employees=None):
    employees = [_result(n) for n in REAL] if employees is None else employees
    script = (HARNESS.replace("EMPLOYEES", json.dumps(employees, ensure_ascii=False)) + _functions()
              + "\n(async()=>{\n" + body + "\n})().catch(e=>{console.error(e);process.exit(1)});\n")
    path = tmp_path / "t.cjs"
    path.write_text(script)
    result = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def test_browser_script_syntax(tmp_path):
    _need_node()
    script = tmp_path / "admin.js"
    script.write_text("\n".join(re.findall(r"<script>(.*?)</script>", ADMIN.read_text(), re.S)))
    subprocess.run(["node", "--check", str(script)], check=True, capture_output=True)


def test_prefill_from_contract(tmp_path):
    _need_node()
    _run(tmp_path, r"""
form=makeForm([]);
const t=await show();
assert.equal(t.cards.length,4);
const [hong,kim,ha,yang]=t.cards;
assert.deepEqual(hong.checkedDays(),[0,1,2,3,6]);
for(const d of [0,1,2,3,6]){assert.equal(hong.ctl('cf_s_'+d).value,'09:00');assert.equal(hong.ctl('cf_e_'+d).value,'21:00');assert.equal(hong.ctl('cf_b_'+d).value,'90')}
assert.equal(hong.ctl('cf_start').value,'09:00');assert.equal(hong.ctl('cf_end').value,'21:00');assert.equal(hong.ctl('cf_break').value,'90');
assert.equal(hong.ctl('date_from').value,'2026-11-01');assert.equal(hong.ctl('date_to').value,'2026-11-30');
assert(!hong.html.includes(NOT_IN));assert(hong.html.includes('계약서 기준'));
for(const [c,s,e] of [[kim,'09:00','21:00'],[ha,'22:00','10:00'],[yang,'21:00','09:00']]){
  assert.deepEqual(c.checkedDays(),[]);assert.equal(c.ctl('cf_start').value,s);assert.equal(c.ctl('cf_end').value,e);assert.equal(c.ctl('cf_break').value,'90');
  assert.equal(c.ctl('cf_s_3').value,s);assert.equal(c.ctl('cf_b_6').value,'90');
}
assert(!hong.html.includes('\x3cdetails open>'));
assert(hong.html.includes('\x3cdetails>\x3csummary>계약서 기준 원문\x3c/summary>'));
assert(hong.html.includes('월 09:00-21:00 (휴게 90분'));
assert(t.innerHTML.includes('서명된 계약서는 바뀌지 않습니다. 보완한 값은 근무표에만 저장됩니다.'));
assert(t.innerHTML.includes('data-cf-save-all'));
assert(hong.html.includes('name=\x22cf_mode_0\x22 value=\x22keep\x22 checked'));
""")


def test_missing_badges_and_highlight(tmp_path):
    _need_node()
    _run(tmp_path, r"""
form=makeForm([]);
const t=await show();const [hong,kim]=t.cards;
assert.equal(countOf(kim.html,NOT_IN),3);
assert(kim.html.includes('계약서에 없는 항목: 근무요일, 계약기간'));
assert(kim.html.includes('\x3cfieldset class=\x22cf-missing\x22>\x3clegend>근무요일 \x3cspan class=\x22badge warn\x22>'));
assert(kim.html.includes('class=\x22field cf-missing\x22 name=\x22date_from\x22'));
assert(!kim.html.includes('class=\x22field cf-missing\x22 name=\x22cf_start\x22'));
assert(kim.html.includes('출근 \x3cspan class=\x22badge\x22>계약서 기준\x3c/span>'));
assert(kim.html.includes('휴게(분) \x3cspan class=\x22badge\x22>계약서 기준\x3c/span>'));
assert(!hong.html.includes('cf-missing'));
apiResult={employees:[{employee_email:'z@x.test',employee_name:'제로',contract_type:'regular',basis:'',pattern:{},
  suggested:{workdays:[],weekdays:{},default:{start_time:null,end_time:null,break_minutes:null,overnight:false}},
  missing:['근무요일','출근시각','퇴근시각','휴게시간','계약기간'],filled_from_contract:{}}]};
const zt=await show();const z=zt.cards[0];
assert.equal(countOf(z.html,NOT_IN),6);
for(const n of ['cf_start','cf_end','cf_break'])assert(z.html.includes('class=\x22field cf-missing\x22 name=\x22'+n+'\x22'));
assert.equal(z.ctl('cf_start').value,'');assert.deepEqual(z.checkedDays(),[]);
let removed=0;const s=z.ctl('cf_start');s.classList={remove(){removed++}};s.value='08:30';
zt.handlers.input({target:s});
assert.equal(removed,1);for(let i=0;i<7;i++)assert.equal(z.ctl('cf_s_'+i).value,'08:30');
""")


def test_per_weekday_times_open_the_grid(tmp_path):
    _need_node()
    terms = {"daily_work_schedule": "월 09:00-18:00 (휴게 60분)\n화 10:00-20:00 (휴게 30분)",
             "start_date": "2026-01-01", "end_date": "2026-12-31"}
    _run(tmp_path, r"""
const c=(await show()).cards[0];
assert(c.html.includes('\x3cdetails open>'));
assert.deepEqual(c.checkedDays(),[0,1]);
assert.equal(c.ctl('cf_s_0').value,'09:00');assert.equal(c.ctl('cf_e_1').value,'20:00');assert.equal(c.ctl('cf_b_1').value,'30');
assert.equal(countOf(c.html,'name=\x22cf_s_'),7);
""", [_result("차등", terms)])


def test_cells_payload_keep_and_overwrite_validates_against_bulk_model(tmp_path):
    _need_node()
    out = _run(tmp_path, r"""
state.workSchedules={month:{rows:[{employee_email:'HONG@x.test',work_date:'2026-11-02'}]}};
form=makeForm([]);
const t=await show();const hong=t.cards[0];
await click(hong.saveButton);
assert.equal(posts().length,1);assert.equal(posts()[0][1].method,'POST');
let body=JSON.parse(posts()[0][1].body);
assert.equal(body.cells.length,21);assert(!body.cells.some(c=>c.work_date==='2026-11-02'));
assert.equal(hong.status.textContent,'21칸 등록 · 1칸 보존');assert.equal(toasts.at(-1),'21칸 등록 · 1칸 보존');
assert.equal(state.scheduleNotice,'21칸 등록 · 1칸 보존');assert.equal(reloads,1);
assert.deepEqual(body.cells[0],{employee_email:'hong@x.test',employee_name:'홍석빈',branch:'',work_date:'2026-11-01',start_time:'09:00',end_time:'21:00',break_minutes:90,kind:'work',memo:'계약서 기준 일정(관리자 보완)'});
const first=body;
hong.ctl('cf_mode_0','keep').checked=false;hong.ctl('cf_mode_0','overwrite').checked=true;
await click(hong.saveButton);
body=JSON.parse(posts()[1][1].body);
assert.equal(body.cells.length,22);assert(body.cells.some(c=>c.work_date==='2026-11-02'));
assert.equal(hong.status.textContent,'22칸 등록 · 0칸 보존');
hong.ctl('cf_weekday',6).checked=false;hong.ctl('cf_s_0').value='10:00';hong.ctl('cf_b_0').value='45';
await click(hong.saveButton);
body=JSON.parse(posts()[2][1].body);
assert.equal(body.cells.length,17);assert(!body.cells.some(c=>c.work_date==='2026-11-01'));
const mon=body.cells.find(c=>c.work_date==='2026-11-09');assert.equal(mon.start_time,'10:00');assert.equal(mon.break_minutes,45);
console.log(JSON.stringify([first,body]));
""")
    for body in json.loads(out.strip().splitlines()[-1]):
        parsed = api.WorkScheduleBulkIn(**body)  # 실제 서버 모델이 받아들이는 모양이어야 한다
        assert len(api.expand_work_schedules(parsed)) == len(body["cells"])


def test_validation_blocks_post(tmp_path):
    _need_node()
    _run(tmp_path, r"""
form=makeForm([]);
const t=await show();const [hong,kim]=t.cards;
await click(kim.saveButton);assert(kim.status.textContent.includes('근무요일을 하나 이상'));
kim.ctl('cf_weekday',1).checked=true;
kim.ctl('cf_s_1').value='09:00';kim.ctl('cf_e_1').value='09:00';
await click(kim.saveButton);assert(kim.status.textContent.includes('출근과 퇴근 시각이 같습니다'));
kim.ctl('cf_e_1').value='21:00';
kim.ctl('cf_b_1').value='-5';await click(kim.saveButton);assert(kim.status.textContent.includes('휴게'));
kim.ctl('cf_b_1').value='';await click(kim.saveButton);assert(kim.status.textContent.includes('휴게'));
kim.ctl('cf_b_1').value='90';kim.ctl('date_from').value='2026-12-01';await click(kim.saveButton);assert(kim.status.textContent.includes('기간을 확인'));
assert.equal(posts().length,0);assert.equal(reloads,0);
kim.ctl('date_from').value='2026-11-01';kim.ctl('cf_s_1').value='22:00';kim.ctl('cf_e_1').value='10:00';
await click(kim.saveButton);
assert.equal(posts().length,1);const b=JSON.parse(posts()[0][1].body);
assert.equal(b.cells.length,4);assert(b.cells.every(c=>c.start_time==='22:00'&&c.end_time==='10:00'&&c.break_minutes===90));
""")


def test_save_all_and_over_1000(tmp_path):
    _need_node()
    _run(tmp_path, r"""
form=makeForm([]);
const t=await show();const [hong,kim,ha,yang]=t.cards;
await click(t.all);
assert.equal(posts().length,0);assert(t.allStatus.textContent.startsWith('김민우: 근무요일을 하나 이상'));
for(const c of [kim,ha,yang])c.ctl('cf_weekday',0).checked=true;
await click(t.all);
assert.equal(posts().length,1);const b=JSON.parse(posts()[0][1].body);
assert.equal(b.cells.length,22+5*3);assert.equal(b.employees.length,4);
assert.equal(t.allStatus.textContent,'37칸 등록 · 0칸 보존');assert.equal(reloads,1);
assert.deepEqual(b.cells.filter(c=>c.employee_email===HA).map(c=>c.start_time),['22:00','22:00','22:00','22:00','22:00']);
for(const c of t.cards){for(let i=0;i<7;i++)c.ctl('cf_weekday',i).checked=true;c.ctl('date_from').value='2026-01-01';c.ctl('date_to').value='2026-12-31'}
await click(t.all);
assert.equal(posts().length,1);assert(t.allStatus.textContent.includes('최대 1,000칸'));
await click(hong.saveButton);assert.equal(posts().length,2);assert.equal(JSON.parse(posts()[1][1].body).cells.length,365);
""")


def test_schedule_bulk_form_is_populated_for_single_selected_employee(tmp_path):
    _need_node()
    _run(tmp_path, r"""
form=makeForm([HONG]);
let t=await show();
assert.deepEqual(form.weekdays.filter(w=>w.checked).map(w=>Number(w.value)),[0,1,2,3,6]);
assert.equal(form.elements.start_time.value,'09:00');assert.equal(form.elements.end_time.value,'21:00');assert.equal(String(form.elements.break_minutes.value),'90');
assert.equal(form.events,1);assert.equal(t.cards.length,1);
assert(t.innerHTML.includes('일정 양식(요일·출근·퇴근·휴게)도 채웠습니다'));
assert.equal(form.elements.start_time.classList.set['cf-missing'],false);
form=makeForm([HA]);
t=await show();
assert.deepEqual(form.weekdays.filter(w=>w.checked),[]);
assert.equal(form.elements.start_time.value,'22:00');assert.equal(form.elements.end_time.value,'10:00');assert.equal(String(form.elements.break_minutes.value),'90');
assert(t.cards[0].html.includes(NOT_IN));
form=makeForm([HONG,HA]);
t=await show();
assert.deepEqual(form.weekdays.filter(w=>w.checked).map(w=>Number(w.value)),[0,1,2,3,4]);
assert.equal(form.elements.start_time.value,'09:00');assert.equal(form.elements.break_minutes.value,'60');assert.equal(form.events,0);
assert.equal(t.cards.length,2);
form=makeForm([]);t=await show();assert.equal(t.cards.length,4);assert.equal(form.events,0);
form=null;t=await show(YANG);assert.equal(t.cards.length,1);assert.equal(t.cards[0].ctl('cf_start').value,'21:00');
""")
