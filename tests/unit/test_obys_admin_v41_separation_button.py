"""ACCT-OBYS-V41-SEPARATION-BUTTON-20261106 — V4.1 admin.html 직원 현황의 퇴사·복직 진입점 정적 검사."""
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[2] / "app/static/apps/obys/admin.html").read_text(encoding="utf-8")


def test_buttons_and_labels():
    assert "data-separate-employee-v41" in HTML
    assert "data-cancel-separation-v41" in HTML
    assert "퇴사·계약해지 처리" in HTML and "복직 처리" in HTML
    assert "퇴사 · 마지막 근무일" in HTML


def test_api_paths_via_finance_api_v41():
    assert "financeApiV41(`/employees/${encodeURIComponent(requestId)}/separation${cancel?\"/cancel\":\"\"}`" in HTML
    for t in ("resignation", "dismissal", "contract_end", "freelancer_termination"):
        assert t in HTML


def test_permission_gate():
    m = re.search(r"function renderEmployeeSeparationV41\(\)\{(.*?)\n", HTML)
    assert m and 'state.canManageOnboarding!==true' in m.group(1)
    o = re.search(r"function openEmployeeSeparationV41\([^)]*\)\{if\(state\.canManageOnboarding!==true\)return", HTML)
    assert o


def test_employment_filter_and_touch_target():
    assert 'id="empEmploymentFilterV41"' in HTML
    for v in ('value="all">전체', 'value="active">재직자', 'value="separated">퇴사자'):
        assert v in HTML
    assert "min-height:44px" in HTML


def test_wired_into_employees_route():
    assert 'if(state.route==="employees"){renderEmployeeOnboardingV41();renderEmployeeSeparationV41()}' in HTML


def _fn(name):
    m = re.search(r"(?:async )?function " + name + r"\(.*?(?=\n    (?:async )?function |\n    document\.addEventListener|\n    const [A-Z_]+V41=)", HTML, re.S)
    assert m, name
    return m.group(0)


def test_click_chain_button_to_dialog():
    # 1) 표의 버튼이 data 속성으로 요청 id 를 싣는다
    assert 'data-separate-employee-v41="${escapeHtml(id)}"' in _fn("paintEmployeeSeparationV41")
    assert 'data-cancel-separation-v41="${escapeHtml(id)}"' in _fn("paintEmployeeSeparationV41")
    # 2) 문서 click 위임이 두 속성을 openEmployeeSeparationV41 로 연결한다
    m = re.search(r'document\.addEventListener\("click",event=>\{(.*?)\}\);\n', HTML, re.S)
    delegate = next(x for x in re.findall(r'document\.addEventListener\("click",event=>\{(.*?)\}\);\n', HTML, re.S) if "openEmployeeSeparationV41" in x)
    assert 'closest?.("[data-separate-employee-v41]")' in delegate
    assert 'openEmployeeSeparationV41(sep.dataset.separateEmployeeV41,false)' in delegate
    assert 'openEmployeeSeparationV41(back.dataset.cancelSeparationV41,true)' in delegate
    # 3) 열기 함수가 dialog 를 만들어 showModal 한다
    body = _fn("openEmployeeSeparationV41")
    assert 'createElement("dialog")' in body and body.rstrip().endswith("dialog.showModal()}")
    assert "/separation${cancel?" in body and "financeApiV41" in body


def test_stale_business_response_is_dropped():
    body = _fn("loadEmployeeSeparationV41")
    assert "const bid=state.businessId" in body and "seq" in body
    # 응답 직후 요청 순서·사업자 일치를 확인해야 한다 (await 뒤에 가드)
    after_await = body.split("await financeApiV41", 1)[1]
    assert "seq!==state.separationSeqV41" in after_await.split("state.separationEmployeesV41=payload", 1)[0]
    assert "bid!==state.businessId" in after_await.split("state.separationEmployeesV41=payload", 1)[0]
    # 다른 사업자 캐시로는 버튼을 그리거나 처리 창을 열지 못한다
    assert "state.separationBidV41===state.businessId" in _fn("paintEmployeeSeparationV41")
    assert "state.separationBidV41===state.businessId" in _fn("openEmployeeSeparationV41")


def test_success_refreshes_existing_panels_and_filter_applies_to_main_table():
    assert "await loadLiveRoute()" in _fn("openEmployeeSeparationV41")
    assert "renderPageV41()" in HTML.split('id="empEmploymentFilterV41"', 1)[1].split("return loadEmployeeSeparationV41", 1)[0]
    assert "function employmentMatchV41(r)" in HTML
    assert ".filter(r=>employmentMatchV41(r))" in HTML


def test_employment_filter_hides_rows_until_cache_and_unmatched_rows():
    # 리뷰 지적(runner-a2e9171d): 캐시 전에는 모든 행을 통과시켰고, 승인 목록에 없는 행은 어떤 필터에서도 남았다.
    body = _fn("employmentMatchV41")
    assert 'if(f==="all"||state.route!=="employees")return true;' in body
    assert "if(state.separationBidV41!==state.businessId||!state.separationEmployeesV41)return false;" in body
    assert 'return !!emp&&(emp.employment_status||"active")===f}' in body
    assert "return !emp||" not in body


def test_main_table_rerenders_after_separation_cache_loads():
    # 캐시가 도착하면 기본 표를 다시 그려 필터를 적용한다(캐시가 있으면 재조회하지 않으므로 반복되지 않는다).
    body = _fn("loadEmployeeSeparationV41")
    assert 'if((state.employmentFilterV41||"all")!=="all")renderPageV41();paintEmployeeSeparationV41()}' in body
    render = _fn("renderEmployeeSeparationV41")
    assert "if(state.separationBidV41===state.businessId&&state.separationEmployeesV41){paintEmployeeSeparationV41();return}" in render
