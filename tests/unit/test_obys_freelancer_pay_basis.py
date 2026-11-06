"""3.3% 프리랜서 지급 기준(시급·일급·월정액·건별) 확장 회귀."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core import obys_tenant
from app.services import obys_contract_clauses
from app.services import yeoljeong_contract_pdf as pdf
from app.services import yeoljeong_finance_service as svc

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app/static/apps/obys"

BASE = dict(
    contract_type="freelancer", employment_tax_type="freelancer_33", wage_type="hourly", wage=12500,
    employee_request_id="req", business_id="biz", branch="시험점", employee_name="가상직원",
    employee_email="test@example.com", employee_phone="010-0000-0000", employee_birth_date="1990-01-01",
    employee_address="서울", employer_name="가상상호", employer_registration_no="123-45-67890",
    employer_representative="대표", employer_address="서울", contract_date="2026-11-06",
    start_date="2026-11-01", freelancer_scope="매장 운영 지원 용역",
    freelancer_settlement_terms="합의된 방식으로 정산",
)
MISSING_LABEL = {"hourly": "확정 시급", "daily": "확정 일급", "monthly": "월 용역비", "case_fee": "확정 용역비"}


@pytest.mark.parametrize("wage_type", ["hourly", "daily", "monthly", "case_fee"])
def test_four_pay_bases_validate_and_require_positive_amount(wage_type):
    assert svc._validate_contract_payload({**BASE, "wage_type": wage_type})["wage_type"] == wage_type
    with pytest.raises(HTTPException) as exc:
        svc._validate_contract_payload({**BASE, "wage_type": wage_type, "wage": 0})
    assert MISSING_LABEL[wage_type] in str(exc.value.detail)


def test_unknown_pay_basis_rejected_with_new_message():
    with pytest.raises(HTTPException) as exc:
        svc._validate_contract_payload({**BASE, "wage_type": "weekly"})
    assert exc.value.status_code == 400
    assert "시급·일급·월정액·건별" in str(exc.value.detail)


def test_non_freelancer_contract_types_unchanged():
    # 근로계약의 임금 방식은 이번 확장과 무관하다.
    assert svc.FREELANCER_WAGE_TYPES == ("hourly", "daily", "monthly", "case_fee")
    assert svc.FREELANCER_ATTENDANCE_WAGE_TYPES == ("hourly", "daily")


def _attendance_context(monkeypatch, wage_type, wage, rows):
    employee = {"id": "req", "email": "test@example.com", "business_id": "biz"}
    monkeypatch.setattr(svc, "_tenant_id", lambda user: "fixture")
    monkeypatch.setattr(svc, "_is_admin", lambda user: True)
    monkeypatch.setattr(svc, "_find_employee_record", lambda *a, **k: employee)
    monkeypatch.setattr(svc, "_require_business_for_tenant", lambda business, user: business)
    monkeypatch.setattr(svc, "_read_hr", lambda *a: [])
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: {
        "contract_id": "c1", "employment": {"contract_type": "freelancer", "wage_type": wage_type, "wage": wage}})
    monkeypatch.setattr(svc, "_payroll_attendance_rows", lambda *a: rows)
    captured = {}

    def fake_save(request, user, server_fields):
        captured.update(request)
        return {**request, **server_fields}, True

    monkeypatch.setattr(svc, "_save_payroll_record", fake_save)
    monkeypatch.setattr(svc, "_audit_payroll", lambda *a: True)
    return captured


def test_hourly_12500_times_approved_minutes_with_33_withholding(monkeypatch):
    captured = _attendance_context(monkeypatch, "hourly", 12500, [
        {"id": "a1", "work_date": "2026-11-02", "worked_minutes": 600, "status": "approved"}])
    svc.payroll_draft_from_attendance({"payroll_month": "2026-11", "employee_request_id": "req"}, {"tenant_id": "fixture"})
    assert captured["gross_pay"] == 125000
    item = captured["pay_items"][0]
    assert item["label"] == "용역비(시급)"
    assert item["calc_note"] == "시급 12,500원 × 승인 600분(분 단위 환산, 원 미만 반올림)"
    by_code = {i["code"]: i["amount"] for i in captured["deduction_items"]}
    assert by_code == {"income_tax": 3750, "local_income_tax": 370}
    assert captured["tax_withholding"] == 4120 and captured["insurance_deduction"] == 0
    assert captured["employment_tax_type"] == "freelancer_33"
    assert captured["gross_pay"] - captured["tax_withholding"] == 120880


def test_hourly_minutes_are_rounded_to_won(monkeypatch):
    captured = _attendance_context(monkeypatch, "hourly", 12500, [
        {"id": "a1", "work_date": "2026-11-02", "worked_minutes": 37, "status": "approved"}])
    svc.payroll_draft_from_attendance({"payroll_month": "2026-11", "employee_request_id": "req"}, {"tenant_id": "fixture"})
    assert captured["gross_pay"] == (37 * 12500 + 30) // 60 == 7708


def test_daily_rate_times_distinct_approved_days(monkeypatch):
    captured = _attendance_context(monkeypatch, "daily", 80000, [
        {"id": "a1", "work_date": "2026-11-02", "worked_minutes": 300, "status": "approved"},
        {"id": "a2", "work_date": "2026-11-02", "worked_minutes": 120, "status": "approved"},
        {"id": "a3", "work_date": "2026-11-03", "worked_minutes": 480, "status": "approved"}])
    svc.payroll_draft_from_attendance({"payroll_month": "2026-11", "employee_request_id": "req"}, {"tenant_id": "fixture"})
    assert captured["gross_pay"] == 160000
    item = captured["pay_items"][0]
    assert item["label"] == "용역비(일급)" and item["calc_note"] == "일급 80,000원 × 승인 2일"
    assert captured["tax_withholding"] == 5280 and captured["insurance_deduction"] == 0


@pytest.mark.parametrize("wage_type", ["monthly", "case_fee"])
def test_attendance_draft_refuses_non_attendance_bases(monkeypatch, wage_type):
    _attendance_context(monkeypatch, wage_type, 2000000, [])
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_attendance({"payroll_month": "2026-11", "employee_request_id": "req"}, {"tenant_id": "fixture"})
    assert exc.value.status_code == 400


def _contract_context(monkeypatch, wage_type, wage):
    employee = {"id": "req", "email": "test@example.com", "business_id": "biz", "name": "가상직원"}
    monkeypatch.setattr(svc, "_tenant_id", lambda user: "fixture")
    monkeypatch.setattr(svc, "_is_admin", lambda user: True)
    monkeypatch.setattr(svc, "_find_employee_record", lambda *a, **k: employee)
    monkeypatch.setattr(svc, "_require_business_for_tenant", lambda business, user: business)
    monkeypatch.setattr(svc, "_read_hr", lambda *a: [])
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: {
        "contract_id": "c1", "employment": {"contract_type": "freelancer", "wage_type": wage_type, "wage": wage,
                                            "start_date": "2026-10-01"}})
    monkeypatch.setattr(svc, "_record_business_id", lambda record: "biz")
    monkeypatch.setattr(svc, "_serialized_employee_write", lambda email, operation, *a: operation())
    captured = {}

    def fake_save(request, user, server_fields=None, employment_override=None):
        captured.update(request)
        captured["server_fields"] = server_fields
        return {**request, **(server_fields or {})}, True

    monkeypatch.setattr(svc, "_save_payroll_record", fake_save)
    return captured


def test_monthly_contract_amount_becomes_gross_without_manual_entry(monkeypatch):
    captured = _contract_context(monkeypatch, "monthly", 2000000)
    svc.payroll_draft_from_contract(
        {"payroll_month": "2026-11", "employee_request_id": "req", "business_id": "biz"}, {"tenant_id": "fixture"})
    assert captured["gross_pay"] == 2000000
    assert captured["pay_items"][0]["label"] == "용역비(월정액)"
    assert captured["pay_items"][0]["calc_note"] == "월 용역비 2,000,000원(계약 금액)"
    assert captured["tax_withholding"] == 66000 and captured["insurance_deduction"] == 0
    assert captured["server_fields"]["settlement_basis"]["wage_type"] == "monthly"


def test_case_fee_still_requires_confirmed_amount(monkeypatch):
    _contract_context(monkeypatch, "case_fee", 136500)
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_contract(
            {"payroll_month": "2026-11", "employee_request_id": "req", "business_id": "biz"}, {"tenant_id": "fixture"})
    assert exc.value.status_code == 400
    assert "확정 용역비" in str(exc.value.detail)


def test_monthly_defaults_plan_uses_contract_amount():
    plan = svc._payroll_defaults_from_employment({
        "contract_id": "c1",
        "employment": {"contract_type": "freelancer", "wage_type": "monthly", "wage": 2000000,
                       "employment_tax_type": "freelancer_33"}})
    assert plan["defaults"]["employment_tax_type"] == "freelancer_33"
    assert plan["defaults"]["gross_pay"] == 2000000 and plan["defaults"]["taxable_pay"] == 2000000
    assert plan["defaults"]["non_tax_meal_allowance"] == 0


def test_min_wage_warning_is_non_blocking():
    assert svc.MIN_WAGE_2026 == 10320
    warning = svc.freelancer_pay_basis_warnings("freelancer", "hourly", 9000)
    assert warning and "10,320" in warning[0]
    assert svc.freelancer_pay_basis_warnings("freelancer", "hourly", 12500) == []
    assert svc.freelancer_pay_basis_warnings("freelancer", "daily", 9000) == []
    # 최저임금 미달이어도 저장 검증은 통과한다(막지 않는다).
    assert svc._validate_contract_payload({**BASE, "wage": 9000})["wage"] == 9000


def test_labor_risk_notice_rules():
    notice = svc.FREELANCER_LABOR_RISK_NOTICE
    assert "노무 확인 권장" in notice
    assert svc.freelancer_labor_risk_notice("freelancer", "hourly") == notice
    assert svc.freelancer_labor_risk_notice("freelancer", "daily") == notice
    assert svc.freelancer_labor_risk_notice("freelancer", "case_fee") == ""
    assert svc.freelancer_labor_risk_notice("freelancer", "monthly") == ""
    assert svc.freelancer_labor_risk_notice("freelancer", "case_fee", {"weekdays": [0, 2]}) == notice
    assert svc.freelancer_labor_risk_notice("part_time", "hourly", {"weekdays": [0]}) == ""


def test_work_pattern_normalization():
    assert svc._normalize_work_pattern({"weekdays": [4, 0, 0, 2], "start_time": "09:00", "end_time": "18:00"}) == {
        "weekdays": [0, 2, 4], "start_time": "09:00", "end_time": "18:00"}
    assert svc._normalize_work_pattern({}) == {"weekdays": [], "start_time": "", "end_time": ""}
    for bad in ({"weekdays": [7]}, {"weekdays": [-1]}, {"weekdays": ["1"]}, {"weekdays": [True]}, {"weekdays": "월"},
                {"weekdays": [1], "start_time": "09:00"}, {"weekdays": [1], "start_time": "9", "end_time": "18:00"},
                {"weekdays": [], "start_time": "09:00", "end_time": "18:00"}):
        with pytest.raises(HTTPException) as exc:
            svc._normalize_work_pattern(bad)
        assert exc.value.status_code == 400


def test_set_work_pattern_saves_on_employee_record_and_audits(monkeypatch):
    employee = {"id": "req12345", "email": "test@example.com", "business_id": "biz", "tenant_id": "fixture"}
    written, audits = [], []
    monkeypatch.setattr(svc, "_employee_for_admin_or_403", lambda *a, **k: employee)
    monkeypatch.setattr(svc, "_read_hr", lambda name, user: [employee] if name == "employee_join_requests" else [])
    monkeypatch.setattr(svc, "_write_hr_record", lambda name, record, user: written.append((name, record)))
    monkeypatch.setattr(svc, "_append_employment_audit", lambda **kw: audits.append(kw))
    monkeypatch.setattr(svc, "_serialized_employee_write", lambda email, operation, *a: operation())
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: {
        "contract_id": "c1", "employment": {"contract_type": "freelancer", "wage_type": "case_fee"}})
    result = svc.set_employee_work_pattern(
        "req12345", {"weekdays": [0, 2, 4], "start_time": "10:00", "end_time": "15:00"}, {"email": "boss@example.com"})
    assert result["work_pattern"]["weekdays"] == [0, 2, 4]
    assert result["labor_risk_notice"] == svc.FREELANCER_LABOR_RISK_NOTICE
    assert written[0][0] == "employee_join_requests" and written[0][1]["work_pattern"]["start_time"] == "10:00"
    assert audits[0]["action"] == "attendance.work_pattern_set"
    # 계약서 레코드는 건드리지 않는다.
    assert all(name != "contracts" for name, _ in written)


def test_work_pattern_requires_admin(monkeypatch):
    monkeypatch.setattr(svc, "_tenant_id", lambda user: "fixture")
    monkeypatch.setattr(svc, "_is_admin", lambda user: False)
    with pytest.raises(HTTPException) as exc:
        svc.set_employee_work_pattern("req12345", {"weekdays": [1]}, {"email": "staff@example.com"})
    assert exc.value.status_code == 403


def test_work_pattern_gaps_lists_scheduled_days_without_attendance(monkeypatch):
    employee = {"id": "req12345", "email": "test@example.com", "business_id": "biz", "tenant_id": "fixture",
                "work_pattern": {"weekdays": [0], "start_time": "", "end_time": ""}}
    monkeypatch.setattr(svc, "_employee_for_admin_or_403", lambda *a, **k: employee)
    monkeypatch.setattr(svc, "_read_hr", lambda *a: [])
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: {
        "contract_id": "c1", "employment": {"contract_type": "freelancer", "wage_type": "daily", "start_date": "2026-01-01"}})
    monkeypatch.setattr(svc, "_payroll_attendance_rows", lambda *a: [
        {"work_date": "2026-01-05", "check_in_at": "2026-01-05T09:00:00+09:00", "status": "approved"},
        {"work_date": "2026-01-12", "check_in_at": "2026-01-12T09:00:00+09:00", "status": "rejected"}])
    result = svc.employee_work_pattern_gaps("req12345", "2026-01", {"email": "boss@example.com"})
    assert result["scheduled_days"] == ["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26"]
    assert result["missing_days"] == ["2026-01-12", "2026-01-19", "2026-01-26"]


def test_work_pattern_without_weekdays_has_no_gaps(monkeypatch):
    employee = {"id": "req12345", "email": "test@example.com", "business_id": "biz", "tenant_id": "fixture"}
    monkeypatch.setattr(svc, "_employee_for_admin_or_403", lambda *a, **k: employee)
    result = svc.employee_work_pattern_gaps("req12345", "2026-01", {"email": "boss@example.com"})
    assert result["scheduled_days"] == [] and result["missing_days"] == []


@pytest.mark.parametrize("wage_type,label", [("daily", "일급"), ("monthly", "월 용역비")])
def test_contract_pdf_and_clauses_follow_pay_basis(wage_type, label):
    contract = {**BASE, "wage_type": wage_type, "wage": 80000}
    text = "\n".join(f"{t} {b}" for t, b in obys_contract_clauses.contract_clauses(contract))
    assert "3.3% 프리랜서 원천징수 · " + ("일급" if wage_type == "daily" else "월정액") in text
    assert "기본급" not in text and "식대" not in text
    rows = dict(pdf.term_rows({"contract_type": "freelancer", "wage_type": wage_type, "wage": 80000, "base_salary": 100,
                               "employment_tax_type": "freelancer_33"}))
    assert "기본급" not in rows
    assert any(label in str(key) for key in rows), rows


def test_tenant_whitelist_opens_new_routes_only_for_expected_methods():
    base = "/api/v1/yeoljeong-finance/employees/approved/req12345/work-pattern"
    wl = obys_tenant._OWNER_CONTRACT_WHITELIST
    assert obys_tenant._matches_whitelist(wl, "GET", base)
    assert obys_tenant._matches_whitelist(wl, "PUT", base)
    assert obys_tenant._matches_whitelist(wl, "GET", base + "/gaps")
    assert not obys_tenant._matches_whitelist(wl, "DELETE", base)
    assert not obys_tenant._matches_whitelist(wl, "POST", base)
    assert not obys_tenant._matches_whitelist(wl, "PUT", base + "/gaps")


def test_routes_registered():
    from app.api import obys_finance

    paths = {(m, r.path) for r in obys_finance.router.routes for m in getattr(r, "methods", set())}
    suffix = "/employees/approved/{request_id}/work-pattern"
    assert ("GET", suffix) in {(m, p[len(p) - len(suffix):]) for m, p in paths if p.endswith(suffix)}
    assert ("PUT", suffix) in {(m, p[len(p) - len(suffix):]) for m, p in paths if p.endswith(suffix)}
    assert any(p.endswith(suffix + "/gaps") for _, p in paths)


def _read(path):
    return (STATIC / path).read_text(encoding="utf-8")


def test_contract_core_accepts_four_bases_and_keeps_fixed_schedule_block():
    core = _read("modules/contract-core.js")
    assert 'FREELANCER_WAGE_TYPES = ["hourly", "daily", "monthly", "case_fee"]' in core
    assert "FREELANCER_WAGE_TYPES.includes(draft.wageType)" in core
    assert 'draft.wageType !== "case_fee"' not in core
    assert "freelancerMissingLabels[draft.wageType]" in core
    assert "고정 근무표·출퇴근·상시 교대" in core
    assert "FREELANCER_WAGE_TYPES, freelancerWageLabels" in core


def test_editor_no_longer_forces_case_fee_and_shows_badges_without_inline_style():
    editor = _read("modules/contract-editor-v41.js")
    assert '!C.FREELANCER_WAGE_TYPES.includes(v.wageType)' in editor
    assert 'v.wageType !== "case_fee"' not in editor
    assert "isSalaryMonthly" in editor and "freelancerWageLabels" in editor
    # 경고 전문을 title 속성이 아니라 화면 본문에 직접 표시한다(터치·키보드 사용자도 읽을 수 있어야 한다).
    risk = editor[editor.index("function riskBadge"): editor.index("\n", editor.index("function riskBadge"))]
    assert "labor_risk_notice" in risk and "title=" not in risk
    assert ">${esc(contract.labor_risk_notice)}</span>" in risk
    # 이번에 추가한 배지·요약 마크업에는 inline style 을 쓰지 않는다.
    for fragment in ("function riskBadge", "function warnBadge"):
        start = editor.index(fragment)
        assert "style=" not in editor[start: editor.index("\n", start)]


def test_autofill_is_basis_aware_with_non_blocking_warnings():
    auto = _read("modules/contract-autofill.js")
    start = auto.index("function computeFreelancer(")
    body = auto[start: auto.index("\n  }\n", start)]
    assert "FREELANCER_BASIS[input.wageType]" in body
    assert "blockers.push" in body and body.count("blockers.push") == 1  # 기간 역전만 차단한다
    assert "FREELANCER_LABOR_RISK_NOTICE" in body and "MIN_WAGE_2026" in body


def test_legacy_index_html_keeps_edit_flow_order_and_allows_four_bases():
    html = _read("index.html")
    assert 'const FREELANCER_WAGE_TYPES = ["hourly", "daily", "monthly", "case_fee"]' in html
    assert "FREELANCER_WAGE_TYPES.includes(draft.wageType)" in html
    assert 'FREELANCER_MISSING_LABELS[draft.wageType]' in html
    fill = html[html.index("function fillContractForm("):]
    fill = fill[: fill.index("function clearPayrollForm")]
    assert fill.index("syncFreelancerWageOptions(") < fill.index("els.contractForm.wageType.value")
    assert "FREELANCER_HOURLY_AUTO_TEXT_RE" in html and "autoHourly" not in html


def test_work_pattern_module_and_admin_wiring():
    module = _read("modules/work-pattern.js")
    assert "work-pattern" in module and "ObysWorkPattern" in module
    assert "style=" not in module
    admin = _read("admin.html")
    assert "modules/work-pattern.js" in admin
    assert 'id="workPatternHost"' in admin and "ObysWorkPattern.mount" in admin


def test_payroll_admin_shows_labor_risk_badge():
    js = _read("modules/payroll-admin.js")
    assert "data-payroll-risk" in js and "labor_risk_notice" in js


def test_payroll_admin_monthly_settlement_uses_contract_amount_without_gross_pay():
    js = _read("modules/payroll-admin.js")
    action = js[js.index('if (action === "settlement")'): js.index('if (action === "draft")')]
    assert 'plan.wage_type === "monthly"' in action and "useContractAmount" in action
    branch = action[action.index("if (useContractAmount)"): action.index("if (!gross ||")]
    # 계약 금액 분기는 gross_pay·amount_confirmed 를 보내지 않아 서버의 월정액 근거 문구가 저장된다.
    assert "gross_pay" not in branch and "amount_confirmed" not in branch
    assert "/payroll/draft-from-contract" in branch
    # 건별 등 다른 기준은 기존대로 확인한 금액을 보낸다.
    assert "gross_pay:Number(gross),amount_confirmed:true" in action


def test_work_pattern_keeps_unsaved_edits_across_rerender():
    module = _read("modules/work-pattern.js")
    assert "state.draft || state.pattern" in module
    assert "host.addEventListener('input', keepDraft)" in module
    assert "host.addEventListener('change', keepDraft)" in module
    assert "draft: null" in module  # 저장에 성공한 직원의 초안만 비운다
