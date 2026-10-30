"""전용 근태·급여 화면과 기존 자료의 소유 테넌트 경계를 검증한다."""
from pathlib import Path
from unittest.mock import AsyncMock

import asyncio

from app.api import obys_workspaces as api
from app.core.obys_tenant import _DEFAULT_LEGACY_TENANT_IDS


def test_legacy_payroll_exact_mapping_and_foreign_tenant(monkeypatch):
    asyncio.run(_check_legacy_scope(monkeypatch))


async def _check_legacy_scope(monkeypatch):
    owner = {"tenant_id": _DEFAULT_LEGACY_TENANT_IDS[0]}
    business = AsyncMock(return_value={"id": "v41-mia", "name": "열정국밥_미아점", "registration_no": "8742102160"})
    fetch = AsyncMock(return_value=[{"id": "legacy-1", "business_id": "biz-mia", "gross_pay": 123, "tax_withholding": 1, "insurance_deduction": 2, "other_deduction": 3}])
    monkeypatch.setattr(api, "_business", business)
    monkeypatch.setattr(api, "_fetch_legacy_people", fetch)
    rows = await api._legacy_people_rows(owner, "v41-mia", "payroll")
    assert rows[0]["id"] == "legacy-1"
    assert "business_id=$1" in fetch.call_args.args[0]
    assert fetch.call_args.kwargs["parameters"][:2] == ("biz-mia", owner["tenant_id"])
    assert rows[0]["total_deductions"] == 6
    fetch.reset_mock()
    assert await api._legacy_people_rows({"tenant_id": "foreign"}, "v41-mia", "payroll") == []
    fetch.assert_not_called()
    business.return_value = {"id": "unmapped", "name": "unknown"}
    assert await api._legacy_people_rows(owner, "unmapped", "payroll") == []
    fetch.assert_not_called()


def test_legacy_mapping_uses_same_business_id_when_canonical_registration_is_blank(monkeypatch):
    async def check():
        owner = {"tenant_id": _DEFAULT_LEGACY_TENANT_IDS[0]}
        # 운영 실측(2026-10-30): V4.1 성신여대점은 id 가 biz-sungshin 이고 등록번호가 있지만,
        # 기존 사업자 목록의 biz-sungshin 등록번호는 비어 있어 등록번호 비교만으로는 연결되지 않았다.
        business = AsyncMock(return_value={"id": "biz-sungshin", "name": "열정국밥 성신여대점", "registration_no": "870-13-02784"})
        fetch = AsyncMock(return_value=[{"id": "legacy-s1", "business_id": "biz-sungshin"}])
        monkeypatch.setattr(api, "_business", business)
        monkeypatch.setattr(api, "_fetch_legacy_people", fetch)
        rows = await api._legacy_people_rows(owner, "biz-sungshin", "payroll")
        assert [row["id"] for row in rows] == ["legacy-s1"]
        assert fetch.call_args.kwargs["parameters"][:2] == ("biz-sungshin", owner["tenant_id"])
        fetch.reset_mock()
        # 같은 id 라도 기존 목록 등록번호가 있는데 다르면 연결하지 않는다.
        business.return_value = {"id": "biz-mia", "name": "열정국밥_미아점", "registration_no": "111-11-11111"}
        assert await api._legacy_people_rows(owner, "biz-mia", "payroll") == []
        fetch.assert_not_called()
        # 다른 테넌트는 같은 id 여도 조회하지 않는다.
        business.return_value = {"id": "biz-sungshin", "name": "열정국밥 성신여대점", "registration_no": "870-13-02784"}
        assert await api._legacy_people_rows({"tenant_id": "foreign"}, "biz-sungshin", "payroll") == []
        fetch.assert_not_called()
    asyncio.run(check())


def test_dedicated_people_views_do_not_render_generic_guidance():
    html = Path("app/static/apps/obys/admin.html").read_text()
    renderer = html.split("function renderPeopleV41(c){", 1)[1].split("function renderPageV41(){", 1)[0]
    assert "업무별 상세 탐색" not in renderer
    assert "핵심 관점" not in renderer
    for label in ("오늘 현황판", "직원별 현황", "출근 달력", "통계", "급여명세 상세"):
        assert label in renderer
    assert 'renderPeopleV41(c);return' in html
    assert "기존 관리화면에서 처리" not in html
    assert 'index.html?legacy=1' not in html


def test_merge_normalizes_sorts_deduplicates_before_limit():
    rows = ({"id": "old", "work_date": "2026-09-01"},)
    legacy = [{"id": "new", "work_date": "2026-10-30"}, {"id": "old", "work_date": "2020-01-01"}]
    assert api._merge_people_rows(rows, legacy, "attendance", 1)[0]["id"] == "new"
    assert len(api._merge_people_rows(rows, legacy, "attendance")) == 2
    assert api._merge_people_rows([{"id": "a", "payroll_month": "2025-12"}],
                                 [{"id": "b", "payroll_month": "2026-09"}], "payroll")[0]["id"] == "b"


def test_kst_month_and_payroll_display_contract():
    html = Path("app/static/apps/obys/admin.html").read_text()
    assert 'peopleMonth:kstDateV41().slice(0,7)' in html
    assert 'timeZone:"Asia/Seoul"' in html
    for key in ("총지급액", "공제", "실수령"):
        assert f'"{key}":payrollMoneyV41(' in html
    assert 'function metricHtml(' in html
    assert 'function openDetailV41(' in html
    assert '판정 자료 없음' in html


def test_attendance_filters_are_bound_and_empty_email_is_not_guessed(monkeypatch):
    async def check():
        monkeypatch.setattr(api, "_business", AsyncMock(return_value={"name": "열정국밥_미아점", "registration_no": "8742102160"}))
        fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(api, "_fetch_legacy_people", fetch)
        await api._legacy_people_rows({"tenant_id": _DEFAULT_LEGACY_TENANT_IDS[0]}, "v41-mia", "attendance",
                                     "2026-10-01", "2026-10-30", "person@example.test", 10)
        assert fetch.call_args.kwargs["parameters"][2:] == ("2026-10-01", "2026-10-30", "person@example.test", 10)
        sql = fetch.call_args.args[0]
        assert "lower(to_jsonb(h)->>'employee_email')=$5" in sql
        assert "ORDER BY work_date DESC" in sql
        assert "person@example.test" not in sql
        assert sql == api.LEGACY_PEOPLE_SQL["attendance"]
    asyncio.run(check())


def test_legacy_sql_is_fixed_literal_and_payroll_month_bound_keeps_month(monkeypatch):
    """SQL 은 라우트별 고정 문자열이고, 급여월(TEXT 'YYYY-MM')은 월 단위로 비교한다."""
    assert set(api.LEGACY_PEOPLE_SQL) == {"payroll", "attendance"}
    for sql in api.LEGACY_PEOPLE_SQL.values():
        assert "{" not in sql and "biz-" not in sql
        assert "$6" in sql

    async def check():
        monkeypatch.setattr(api, "_business", AsyncMock(return_value={"name": "열정국밥_미아점", "registration_no": "8742102160"}))
        fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(api, "_fetch_legacy_people", fetch)
        await api._legacy_people_rows({"tenant_id": _DEFAULT_LEGACY_TENANT_IDS[0]}, "v41-mia", "payroll",
                                     "2026-09-01", "2026-09-30")
        assert fetch.call_args.args[0] == api.LEGACY_PEOPLE_SQL["payroll"]
        assert "left(payroll_month,7)>=$3" in fetch.call_args.args[0]
        assert fetch.call_args.kwargs["parameters"][2:4] == ("2026-09", "2026-09")
    asyncio.run(check())


def test_payroll_records_endpoint_merges_only_owner_legacy(monkeypatch):
    async def check():
        from types import SimpleNamespace
        business = {"id": "v41-mia", "name": "열정국밥_미아점", "registration_no": "8742102160"}
        monkeypatch.setattr(api, "_business", AsyncMock(return_value=business))
        monkeypatch.setattr(api, "_require_route_read", lambda *args: None)
        connection = SimpleNamespace(fetch=AsyncMock(return_value=()), close=AsyncMock())
        monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=connection))
        fetch = AsyncMock(return_value=[{"id": "legacy-1", "business_id": "biz-mia", "payroll_month": "2026-09", "gross_pay": 100, "employee_name": "직원"}])
        monkeypatch.setattr(api, "_fetch_legacy_people", fetch)
        kwargs = dict(business_id="v41-mia", route="payroll", date_from=None, date_to=None,
                      status="전체 상태", search="", limit=500, offset=0)
        owner = await api.workspace_records(**kwargs, current_user={"tenant_id": _DEFAULT_LEGACY_TENANT_IDS[0]})
        assert owner["count"] == 1
        assert owner["records"][0]["raw"]["legacy_source"] is True
        assert owner["source"]["name"].endswith("+acct_legacy")
        fetch.reset_mock()
        foreign = await api.workspace_records(**kwargs, current_user={"tenant_id": "foreign"})
        assert foreign["count"] == 0
        assert foreign["source"]["name"] == "yeoljeong_payroll_statements"
        fetch.assert_not_called()
    asyncio.run(check())


def test_people_renderer_executes_payroll_and_all_attendance_tabs(tmp_path):
    import shutil
    import subprocess
    import pytest
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    html = Path("app/static/apps/obys/admin.html").read_text()
    prefixes = ("function escapeHtml(", "function metricHtml(", "function payrollMoneyV41(",
                "function payrollDisplayV41(", "function attendanceStatusV41(",
                "function attendanceReviewV41(", "function attendanceMinutesV41(",
                "function attendanceDisplayV41(", "function liveRow(", "function filteredRows(c)",
                "function peopleWorkLinksV41(", "function peopleMonthBoundsV41(",
                "function workSchedulePanelV41(")
    helpers = "\n".join(next(line.strip() for line in html.splitlines() if line.strip().startswith(prefix)) for prefix in prefixes)
    renderer = "function renderPeopleV41(c){" + html.split("function renderPeopleV41(c){", 1)[1].split("function renderPageV41(){", 1)[0]
    script = tmp_path / "people-render.cjs"
    script.write_text('''const assert=require('node:assert/strict');
const state={route:'payroll',peopleMonth:'2026-09',readRole:'member',status:'전체 상태',selected:{},search:''};
const configs={payroll:{title:'급여 현황',columns:['직원','총지급액','공제','실수령','상태'],hierarchy:[]},attendance:{title:'근태 현황',columns:['직원','일자','지점','근무시간','상태'],hierarchy:[]}};
const element={};const elements={};const document={getElementById:id=>elements[id]||(elements[id]={addEventListener(){}})};
function bindWorkSchedulesV41(){};
const page={innerHTML:'',querySelectorAll:()=>[]};
const kstDateV41=()=>'2026-09-30';function renderPageV41(){};function loadLiveRoute(){};function openDetailV41(){};
''' + helpers + renderer + '''
configs.payroll.data=[liveRow({id:'p1',raw:{payroll_month:'2026-09',gross_pay:3000000,total_deductions:300000,net_pay:2700000,status:'draft'},display:{직원:'직원A'}},configs.payroll)];
renderPeopleV41(configs.payroll);
for(const value of ['3,000,000원','300,000원','2,700,000원','확정 전'])assert(page.innerHTML.includes(value),value);
state.peoplePerson='p1';renderPeopleV41(configs.payroll);assert(page.innerHTML.includes('급여명세 상세'));
state.route='attendance';configs.attendance.data=[liveRow({id:'a1',raw:{work_date:'2026-09-30',start_at:'09:00',end_at:'18:00',worked_minutes:480,status:'approved'},display:{직원:'직원A',일자:'2026-09-30'}},configs.attendance)];
for(const tab of ['today','employees','detail','schedule','stats']){
 state.peopleTab=tab;renderPeopleV41(configs.attendance);
 assert(!page.innerHTML.includes('업무별 상세 탐색'));assert(!page.innerHTML.includes('핵심 관점'));
 assert(!page.innerHTML.includes('undefined'));
 if(tab==='stats')assert(page.innerHTML.includes('8시간 0분'));
 if(tab==='today'){assert(page.innerHTML.includes('직원A'));assert(page.innerHTML.includes('근무표 미등록 — 근무 일정 탭에서 등록'));}
}
configs.attendance.data.push(liveRow({id:'a2',raw:{work_date:'2026-09-29',start_at:'10:00',status:'approved'},display:{직원:'직원B',일자:'2026-09-29'}},configs.attendance));
state.peopleTab='today';renderPeopleV41(configs.attendance);
assert(page.innerHTML.includes('미출근'));assert(!page.innerHTML.includes('직원B')); // 기록 이력만으로 미출근을 추측하지 않는다.
state.workSchedules={month:{rows:[],counts:{}},today:{counts:{미출근:1},rows:[{employee_key:'staff-b',employee_name:'직원B',judgment:{status:'미출근',labels:['미출근']},start_time:'09:00',end_time:'18:00'}]}};
renderPeopleV41(configs.attendance);assert(page.innerHTML.includes('직원B'));assert(page.innerHTML.includes('예정 09:00'));
state.workSchedules=null;
// Search must never turn a present employee into a missing employee.
state.search='직원B';renderPeopleV41(configs.attendance);
assert(!page.innerHTML.includes('직원A'));
// Same names use distinct employee emails, payroll details use statement IDs.
state.search='';state.peopleTab='detail';state.peoplePerson='one@example.test';
configs.attendance.data=[liveRow({id:'same1',employee_key:'one@example.test',raw:{employee_email:'one@example.test',work_date:'2026-09-01',status:'approved'},display:{직원:'동명'}},configs.attendance),liveRow({id:'same2',employee_key:'two@example.test',raw:{employee_email:'two@example.test',work_date:'2026-09-02',status:'rejected'},display:{직원:'동명'}},configs.attendance)];
renderPeopleV41(configs.attendance);assert(!page.innerHTML.includes('반려'));
state.route='payroll';state.peoplePerson='p2';
configs.payroll.data.push(liveRow({id:'p2',raw:{payroll_month:'2026-09',gross_pay:777,status:'draft'},display:{직원:'직원A'}},configs.payroll));
renderPeopleV41(configs.payroll);assert(page.innerHTML.split('급여명세 상세')[1].includes('777원'));
// Month changes trigger a fresh request.
elements.peopleMonth.onchange({target:{value:'2025-12'}});assert.equal(state.peopleMonth,'2025-12');

''')
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_legacy_query_is_readonly_bound_and_closes(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from app.services import yeoljeong_finance_service as finance

    async def check():
        transactions = []
        @asynccontextmanager
        async def transaction(**kwargs):
            transactions.append(kwargs)
            yield
        connection = SimpleNamespace(transaction=transaction, fetch=AsyncMock(return_value=[]), close=AsyncMock())
        monkeypatch.setattr(finance, "_db_url", lambda: "postgres://test-only/obys")
        connect = AsyncMock(return_value=connection)
        monkeypatch.setattr(api.asyncpg, "connect", connect)
        await api._fetch_legacy_people("SELECT $1", parameters=("bound-value",))
        assert transactions == [{"readonly": True}]
        connection.fetch.assert_awaited_once_with("SELECT $1", "bound-value", timeout=10)
        connection.close.assert_awaited_once()
        connect.assert_awaited_once_with("postgres://test-only/obys", timeout=5)
    asyncio.run(check())


def test_member_legacy_attendance_uses_own_email_even_if_request_forged(monkeypatch):
    from types import SimpleNamespace
    async def check():
        monkeypatch.setattr(api, "_business", AsyncMock(return_value={"id": "v41-mia", "name": "미아점"}))
        monkeypatch.setattr(api, "_require_route_read", lambda *args: "self@example.test")
        connection = SimpleNamespace(fetch=AsyncMock(return_value=()), close=AsyncMock())
        monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=connection))
        legacy = AsyncMock(return_value=[])
        monkeypatch.setattr(api, "_legacy_people_rows", legacy)
        response = await api.list_attendance_records("v41-mia", limit=10, date_from=None, date_to=None,
                                                    employee_email="other@example.test", current_user={"tenant_id": "owner"})
        assert response["count"] == 0
        assert connection.fetch.call_args.args[4] == "self@example.test"
        # 원천마다 offset+limit 건까지만 읽는다(무제한 조회 금지).
        assert legacy.call_args.args[-2:] == ("self@example.test", 10)
    asyncio.run(check())


def test_month_loader_requests_selected_month_and_every_page(tmp_path):
    import shutil
    import subprocess
    import pytest
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    html = Path("app/static/apps/obys/admin.html").read_text()
    helpers = "\n".join(line.strip() for line in html.splitlines() if line.strip().startswith(("function peopleMonthBoundsV41(", "async function peopleLoadV41(")))
    script = tmp_path / "month.cjs"
    script.write_text("""const assert=require('node:assert/strict');
const state={route:'attendance',businessId:'biz',peopleMonth:'2025-12',peopleMonthSelected:true};
const kstDateV41=()=> '2026-10-30';const calls=[];
async function loadWorkSchedulesV41(business,bounds){assert.equal(business,"biz");assert.equal(bounds.from,"2025-12-01");return {month:{rows:[]},today:{rows:[]}}}
async function workspaceApi(path){calls.push(path);return {records:[{id:calls.length}],has_more:calls.length%2===1,next_offset:calls.length%2===1?500:501}}
""" + helpers + """
(async()=>{let result=await peopleLoadV41();assert.equal(result[1].records.length,2);
assert(calls[0].includes('from=2025-12-01'));assert(calls[0].includes('to=2025-12-31'));assert(calls[1].includes('offset=500'));
state.route='payroll';result=await peopleLoadV41();assert.equal(result[1].records.length,2);assert(calls[2].includes('date_from=2025-12-01'));assert(calls[2].includes('date_to=2025-12-31'));
})().catch(e=>{console.error(e);process.exitCode=1});
""")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_employee_key_survives_masking_and_separates_same_names():
    def record(email):
        return api._record("generic", {"id": email, "employee_name": "동명이인", "employee_email": email}, "사업자")
    first, second = record("one@example.test"), record("two@example.test")
    assert first["raw"]["employee_email"] == "[MASKED]"
    assert first["employee_key"] != second["employee_key"]
    assert first["employee_key"] == record("ONE@example.test")["employee_key"]


def test_attendance_month_paginates_more_than_500_without_truncation(monkeypatch):
    from types import SimpleNamespace

    async def check():
        monkeypatch.setattr(api, "_business", AsyncMock(return_value={"id": "v41", "name": "사업자"}))
        monkeypatch.setattr(api, "_require_route_read", lambda *args: None)
        monkeypatch.setattr(api, "_attendance_schedule_matches", AsyncMock(return_value={}))
        rows = [{"id": str(i), "employee_email": "same@example.test", "work_date": "2025-12-01"} for i in range(601)]
        connection = SimpleNamespace(fetch=AsyncMock(side_effect=lambda sql, *args, **kw: rows[:args[-1]]),
                                     close=AsyncMock())
        monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=connection))
        monkeypatch.setattr(api, "_legacy_people_rows", AsyncMock(return_value=[]))
        kwargs = dict(business_id="v41", limit=500, date_from="2025-12-01", date_to="2025-12-31",
                      employee_email=None, current_user={})
        first = await api.list_attendance_records(**kwargs, offset=0)
        second = await api.list_attendance_records(**kwargs, offset=first["next_offset"])
        assert first["count"] == 500 and first["has_more"] is True
        assert second["count"] == 101 and second["has_more"] is False
        assert len({r["id"] for r in first["records"] + second["records"]}) == 601
        assert connection.fetch.call_args.args[-1] == 1000  # offset 500 + limit 500 까지만 조회
    asyncio.run(check())
