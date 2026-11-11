"""오비서 근태 화면 가독성 — .attendance-screen 범위 CSS·열 class 검증."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[2] / "app/static/apps/obys/admin.html").read_text(encoding="utf-8")


def _css() -> str:
    return "\n".join(l for l in HTML.splitlines() if ".attendance-screen" in l)


def test_scope_css_font_sizes():
    css = _css()
    assert css
    assert not re.search(r"font-size:\s*(9|10|11)(\.\d+)?px", css)
    assert re.search(r"\.attendance-screen td\{[^}]*font-size:14px", css)
    th = re.search(r"\.attendance-screen th\{[^}]*\}", css).group(0)
    assert "font-size:13px" in th and "font-weight:700" in th and "#475467" in th and "keep-all" in th
    assert re.search(r"\.attendance-screen \.metric small\{[^}]*font-size:13px", css)
    assert re.search(r"\.attendance-screen \.metric span\{[^}]*font-size:12px", css)
    assert re.search(r"\.attendance-screen \.source-status\{[^}]*font-size:12px", css)
    assert re.search(r"\.attendance-screen \.metric strong\{[^}]*font-size:22px", css)
    assert re.search(r"max-width:480px\)\{\.attendance-screen \.metric strong\{font-size:18px", css)
    assert "tabular-nums" in css and "line-height:1.5" in css


def test_mobile_card_rules():
    css = _css()
    t = r"\.attendance-screen \.table-wrap table:not\(#weeklyRoster\)"
    assert re.search(r"@media\(max-width:600px\)\{" + t + r"\{min-width:0;width:100%\}", css)
    assert re.search(t + r" thead\{display:none\}", css)
    assert "display:grid" in css and "min-height:44px" in css
    assert "calendar-grid" not in css


def test_roster_and_calendar_untouched_by_card_rules():
    css = _css()
    # 근무표(#weeklyRoster) 머리글(날짜)은 모바일에서도 유지: thead 숨김·카드 규칙은 모두 :not(#weeklyRoster) 한정
    for m in re.finditer(r"(\.attendance-screen \.table-wrap [^{}]*)\{", css):
        assert ":not(#weeklyRoster)" in m.group(1), m.group(1)
    # 달력 버튼(.calendar-day-act 12px)을 덮는 일반 .btn 규칙 금지
    assert not re.search(r"\.attendance-screen \.btn\b", css)
    assert not re.search(r"\.attendance-screen[^{]*calendar", css)


def test_scope_class_only_for_attendance_route():
    assert '(state.route==="attendance"?" attendance-screen":"")' in HTML


def _render(route: str, tmp_path: Path, html: str | None = None) -> str:
    HTML = html if html is not None else globals()["HTML"]
    renderer = "function renderPeopleV41(c){" + HTML.split("function renderPeopleV41(c){", 1)[1].split("function renderPageV41(){", 1)[0]
    page_fn = "function renderPageV41(){page.className=" + HTML.split("function renderPageV41(){page.className=", 1)[1].split(";", 1)[0] + ";}"
    lines = {l.strip() for l in HTML.splitlines()}
    prefixes = ("function escapeHtml(", "function metricHtml(", "function payrollMoneyV41(", "function payrollDisplayV41(", "function attendanceStatusV41(",
                "function attendanceReviewV41(", "function attendanceMinutesV41(", "function displayDateV41(", "function attendanceTimeV41(", "function attendanceDisplayV41(",
                "function liveRow(", "function employmentMatchV41(", "function filteredRows(c)", "function peopleWorkLinksV41(", "function peopleMonthBoundsV41(",
                "function activeMonthV41(", "function workSchedulePanelV41(", "function payrollLatestMonthV41(", "function payrollMonthRowsV41(",
                "function payrollDetailRowV41(", "function payrollPayloadV41(", "function payrollNumV41(", "function payrollDeductionTotalV41(",
                "function payrollContractIdV41(", "function payrollUnsplitV41(", "function payrollPaymentCheckV41(", "function payrollFlagsV41(",
                "function payrollSummaryV41(", "function payrollSummaryHtmlV41(", "function payrollBasisHtmlV41(", "function payrollColClassV41(",
                "function payrollPayoutV41(", "function payrollPayoutBadgeV41(", "function payrollPayoutOpenCountV41(", "function payrollPayoutPanelV41(",
                "function payrollRowNoteV41(", "function payrollLinkCacheV41(", "function payrollStatusKeyV41(", "function payrollDashV41(",
                "function payrollWorkTextV41(", "function payrollActionV41(", "function payrollListHtmlV41(",
                "function payrollFocusActionV41(", "function payrollSetEmployeeV41(", "async function payrollSelectRowV41(", "function payrollLinksV41(",
                "async function payrollResolveV41(", "function payrollApprovedV41(", "function payrollEditorSelectionV41(")
    pre = [next(l.strip() for l in HTML.splitlines() if l.strip().startswith(pf)) for pf in prefixes]
    assert lines
    js = f"""const state={{route:'{route}',peopleMonth:'2026-11',payrollMonth:'2026-11',readRole:'owner',status:'전체 상태',selected:{{}},search:'',businessId:'b1',peopleTab:'employees'}};
const configs={{payroll:{{title:'급여',columns:['직원','지점','총지급액','공제','실수령','상태'],hierarchy:[]}},attendance:{{title:'근태',columns:['직원','일자','지점','근무시간','상태'],hierarchy:[]}}}};
const el={{}};const document={{getElementById:id=>el[id]||(el[id]={{addEventListener(){{}},dataset:{{}}}}),querySelector:()=>null,createElement:()=>({{}})}};
const page={{innerHTML:'',className:'',insertAdjacentHTML(p,h){{page.innerHTML+=h}},append(){{}},querySelectorAll:()=>[]}};
const kstDateV41=()=>'2026-11-11';const window={{ObysPayrollAdmin:{{mount(){{return Promise.resolve()}}}},ObysAttendanceCorrections:{{mount(){{}}}}}};const workspaceApi=async()=>({{}});const financeApiV41=async()=>({{employees:[]}});const BIZDOC_API_V41='/x';
function bindWorkSchedulesV41(){{}};function loadLiveRoute(){{}};function openDetailV41(){{}};function showToast(){{}};
""" + "\n".join(pre) + "\n" + renderer + "\n" + page_fn + f"""
const c=configs.{route};
c.data=[liveRow({{id:'a1',raw:{{work_date:'2026-11-10',worked_minutes:480,status:'approved',payroll_month:'2026-11',gross_pay:1000,net_pay:900}},display:{{직원:'홍길동',지점:'본점'}}}},c)];
renderPageV41();state.workspaceReady=true;renderPeopleV41(c);
console.log(JSON.stringify({{cls:page.className,html:page.innerHTML}}));
"""
    f = tmp_path / f"{route}.cjs"
    f.write_text(js, encoding="utf-8")
    done = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_attendance_render_classes(tmp_path):
    import json
    out = json.loads(_render("attendance", tmp_path))
    h = out["html"]
    assert "attendance-screen" in out["cls"] and "payroll-screen" not in out["cls"]
    assert '<th class="num">근무시간</th>' in h and '<th class="center">상태</th>' in h
    assert "<th>직원</th>" in h and "<th>일자</th>" in h and "<th>지점</th>" in h
    assert '<td class="num">' in h


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_payroll_render_unchanged(tmp_path):
    import json
    out = json.loads(_render("payroll", tmp_path))
    assert "attendance-screen" not in out["cls"] and "payroll-screen" in out["cls"]
    assert "attendance-screen" not in out["html"]
    h = out["html"]
    # 변경 전(7ce586ff, origin/main) admin.html 로 같은 하네스를 돌려 얻은 급여 렌더 원본 — 한 글자도 바뀌면 안 된다
    assert out["cls"] == PAYROLL_BASELINE_CLS
    assert h == PAYROLL_BASELINE_HTML


PAYROLL_BASELINE_CLS = 'page people-screen payroll-screen'
PAYROLL_BASELINE_HTML = '<div class="hero"><div><h1>급여</h1><p>월별 지급·공제·명세를 확인합니다.</p></div><a class="btn" style="display:flex;min-height:44px;align-items:center" href="/static/apps/obys/clock.html">출퇴근 하기</a></div><div class="source-status" hidden><b></b><span>1건</span><button class="btn" id="peopleRetry">다시 조회</button></div><div class="toolbar payroll-toolbar"><button type="button" class="btn" id="payrollMonthPrev">◀ 이전 달</button><label>급여월<input class="field" type="month" id="peopleMonth" value="2026-11"></label><button type="button" class="btn" id="payrollMonthNext">다음 달 ▶</button><label>직원 검색<select class="field" id="peopleSearch"><option value="">전체 직원</option></select></label><label>지점<select class="field" id="payrollBranch"><option value="">전체 지점</option><option value="지점 미등록" >지점 미등록</option></select></label></div><section class="section" id="payrollSummary" aria-label="2026-11 급여 합계"><div class="metrics"><article class="metric "><small>총지급 합계</small><strong>1,000원</strong><span>2026-11 · 1건</span></article><article class="metric "><small>공제 합계</small><strong>0원</strong><span>항목별 공제 반영</span></article><article class="metric "><small>실수령 합계</small><strong>900원</strong><span>총지급 − 공제 − 보류 · 차액 미처리 0건</span></article><article class="metric "><small>지급 대기 금액</small><strong>0원</strong><span>확정 0건 · 아직 지급 완료 전</span></article><article class="metric warn"><small>확정 대기</small><strong>1건</strong><span>확정 0건 · 계약 미연결 1건</span></article><article class="metric "><small>신고 미처리</small><strong>0건</strong><span>신고 미처리 명세 없음</span></article><article class="metric "><small>보류 금액</small><strong>0원</strong><span>0건 · 누적 보류 잔액 0원(0명) · 지급 예정, 공제 아님</span></article></div></section><section class="section"><div class="section-head"><h2>직원별 급여</h2></div><nav class="work-tabs payroll-tabs" id="payrollTabs" aria-label="급여 상태"><button type="button" class="work-tab " data-payroll-tab="none" aria-pressed="false">명세 없음 0</button><button type="button" class="work-tab " data-payroll-tab="draft" aria-pressed="false">초안 1</button><button type="button" class="work-tab " data-payroll-tab="confirmed" aria-pressed="false">확정 0</button><button type="button" class="work-tab " data-payroll-tab="paid" aria-pressed="false">지급 완료 0</button><button type="button" class="work-tab active" data-payroll-tab="all" aria-pressed="true">전체 1</button></nav><div class="table-wrap payroll-list"><table><thead><tr><th>직원</th><th class="c-branch">지점</th><th class="c-work num">근무시간</th><th class="c-gross num">총지급</th><th class="c-net num">실수령</th><th class="center">상태</th><th class="center">다음 할 일</th></tr></thead><tbody><tr data-status="draft" data-row-person="record:a1"><td class="c-name"><button type="button" class="btn" data-person="record:a1">홍길동</button></td><td class="c-branch">지점 미등록</td><td class="c-work num">-</td><td class="c-gross num">1,000원</td><td class="c-net num">900원</td><td class="c-state center"><span class="badge warn" data-status-badge="draft">확정 전</span> <small class="badge warn" data-flag="no-contract">계약 미연결</small> <small data-link="unmatched">직원 미연결</small></td><td class="c-act center"><button type="button" class="btn primary" data-person="record:a1" data-action="confirm">검토·확정</button></td></tr></tbody></table></div><p><button type="button" class="btn" id="payrollNewStatement">새 급여명세 직접 작성</button></p></section><section class="section" id="payrollEditor"></section>'
