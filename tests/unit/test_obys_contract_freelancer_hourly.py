"""3.3% 프리랜서 시급제 계약 허용 및 근태 기반 급여 초안 회귀."""
import pytest
from fastapi import HTTPException

from app.services import obys_contract_clauses  # noqa: F401
from app.services import yeoljeong_contract_pdf as pdf
from app.services import yeoljeong_finance_service as svc

BASE = dict(
    contract_type="freelancer", employment_tax_type="freelancer_33", wage_type="hourly", wage=13000,
    employee_request_id="req", business_id="biz", branch="시험점", employee_name="가상직원",
    employee_email="test@example.com", employee_phone="010-0000-0000", employee_birth_date="1990-01-01",
    employee_address="서울", employer_name="가상상호", employer_registration_no="123-45-67890",
    employer_representative="대표", employer_address="서울", contract_date="2026-11-05",
    start_date="2026-11-01", freelancer_scope="매장 운영 지원 용역",
    freelancer_settlement_terms="시급 13,000원 × 승인된 근태 시간",
)


def test_freelancer_hourly_accepted_and_unknown_basis_rejected():
    # 2026-11-06: 일급·월정액도 허용된다(tests/unit/test_obys_freelancer_pay_basis.py). 정해지지 않은 기준만 거절한다.
    assert svc._validate_contract_payload(dict(BASE))["wage_type"] == "hourly"
    with pytest.raises(HTTPException) as exc:
        svc._validate_contract_payload({**BASE, "wage_type": "weekly"})
    assert exc.value.status_code == 400


def test_freelancer_hourly_requires_positive_wage():
    with pytest.raises(HTTPException) as exc:
        svc._validate_contract_payload({**BASE, "wage": 0})
    assert "확정 시급" in str(exc.value.detail)


def test_freelancer_case_fee_unchanged():
    out = svc._validate_contract_payload({**BASE, "wage_type": "case_fee", "wage": 136500})
    assert out["wage_type"] == "case_fee"
    with pytest.raises(HTTPException) as exc:
        svc._validate_contract_payload({**BASE, "wage_type": "case_fee", "wage": 0})
    assert "확정 용역비" in str(exc.value.detail)


def test_defaults_do_not_early_return_for_freelancer_hourly(monkeypatch):
    emp = {"contract_id": "c1", "employment": {"contract_type": "freelancer", "wage_type": "hourly", "wage": 13000,
                                                "employment_tax_type": "freelancer_33"}}
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: emp)
    plan = svc._payroll_defaults_from_employment(emp)
    assert plan["defaults"]["employment_tax_type"] == "freelancer_33"
    assert plan["hourly_wage"] == 13000
    assert "근태" in plan["reason"]
    case = svc._payroll_defaults_from_employment(
        {"contract_id": "c2", "employment": {"contract_type": "freelancer", "wage_type": "case_fee", "wage": 136500}})
    assert "건별" in case["reason"]


def test_attendance_draft_withholding_600_minutes(monkeypatch):
    employee = {"id": "req", "email": "test@example.com", "business_id": "biz"}
    user = {"tenant_id": "fixture", "is_admin": True}
    monkeypatch.setattr(svc, "_tenant_id", lambda user: "fixture")
    monkeypatch.setattr(svc, "_is_admin", lambda user: True)
    monkeypatch.setattr(svc, "_find_employee_record", lambda *a, **k: employee)
    monkeypatch.setattr(svc, "_require_business_for_tenant", lambda business, user: business)
    monkeypatch.setattr(svc, "_read_hr", lambda *a: [])
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: {
        "contract_id": "c1", "employment": {"contract_type": "freelancer", "wage_type": "hourly", "wage": 13000}})
    monkeypatch.setattr(svc, "_payroll_attendance_rows", lambda *a: [
        {"id": "a1", "work_date": "2026-11-02", "worked_minutes": 600, "status": "approved"}])
    captured = {}

    def fake_save(request, user, server_fields):
        captured.update(request)
        return {**request, **server_fields}, True

    monkeypatch.setattr(svc, "_save_payroll_record", fake_save)
    monkeypatch.setattr(svc, "_audit_payroll", lambda *a: True)
    svc.payroll_draft_from_attendance({"payroll_month": "2026-11", "employee_request_id": "req"}, user)
    assert captured["gross_pay"] == 130000
    by_code = {i["code"]: i["amount"] for i in captured["deduction_items"]}
    assert by_code == {"income_tax": 3900, "local_income_tax": 390}
    assert captured["tax_withholding"] == 4290 and captured["insurance_deduction"] == 0
    assert captured["gross_pay"] - captured["tax_withholding"] == 125710
    assert captured["employment_tax_type"] == "freelancer_33"


def test_clause_and_pdf_labels_for_hourly_freelancer():
    text = "\n".join(f"{t} {b}" for t, b in obys_contract_clauses.contract_clauses(dict(BASE)))
    assert "3.3% 프리랜서 원천징수 · 시급" in text
    assert "기본급" not in text and "환산 시급" not in text and "식대" not in text
    rows = dict(pdf.term_rows({"contract_type": "freelancer", "wage_type": "hourly", "wage": 13000, "base_salary": 100,
                               "employment_tax_type": "freelancer_33"}))
    assert rows.get("시급") == 13000
    assert "기본급" not in rows


def _run_draft(monkeypatch, existing):
    employee = {"id": "req", "email": "test@example.com", "business_id": "biz"}
    user = {"tenant_id": "fixture", "is_admin": True}
    monkeypatch.setattr(svc, "_tenant_id", lambda user: "fixture")
    monkeypatch.setattr(svc, "_is_admin", lambda user: True)
    monkeypatch.setattr(svc, "_find_employee_record", lambda *a, **k: employee)
    monkeypatch.setattr(svc, "_require_business_for_tenant", lambda business, user: business)
    monkeypatch.setattr(svc, "_read_hr", lambda *a: existing)
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: {
        "contract_id": "c1", "employment": {"contract_type": "freelancer", "wage_type": "hourly", "wage": 13000}})
    monkeypatch.setattr(svc, "_payroll_attendance_rows", lambda *a: [
        {"id": "a1", "work_date": "2026-11-02", "worked_minutes": 600, "status": "approved"}])
    captured = {}

    def fake_save(request, user, server_fields):
        captured.update(request)
        return {**request, **server_fields}, True

    monkeypatch.setattr(svc, "_save_payroll_record", fake_save)
    monkeypatch.setattr(svc, "_audit_payroll", lambda *a: True)
    svc.payroll_draft_from_attendance({"payroll_month": "2026-11", "employee_request_id": "req"}, user)
    return captured


@pytest.mark.parametrize("with_items", [True, False])
def test_regenerated_draft_keeps_other_deduction_consistent(monkeypatch, with_items):
    draft = {"id": "p1", "payroll_month": "2026-11", "employee_email": "test@example.com", "business_id": "biz",
             "status": "draft", "other_deduction": 1000}
    if with_items:
        draft["deduction_items"] = [{"code": "other", "label": "기타", "amount": 1000}]
    captured = _run_draft(monkeypatch, [draft])
    items = captured["deduction_items"]
    assert sum(i["amount"] for i in items if i["code"] == "other") == captured["other_deduction"] == 1000
    # 실제 항목 검증기를 통과해 합계가 필드와 맞는지 확인한다.
    normalized = svc._payroll_payload_with_items(dict(captured))  # 400 이면 HTTPException
    assert normalized["other_deduction"] == 1000
    assert normalized["tax_withholding"] == 4290


def test_index_html_edit_flow_resets_wage_options_and_detects_auto_text():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[2] / "app/static/apps/obys/index.html").read_text(encoding="utf-8")
    fill = html[html.index("function fillContractForm("):]
    fill = fill[: fill.index("function clearPayrollForm")]
    # 1) 편집 진입 시 임금 방식 옵션 제한을 먼저 갱신한 뒤 wageType 을 채운다.
    assert fill.index("syncFreelancerWageOptions(") < fill.index("els.contractForm.wageType.value")
    # 2) 자동 정산 문구는 dataset 이 아니라 문구 형태로 식별한다.
    assert "autoHourly" not in html
    assert "FREELANCER_HOURLY_AUTO_TEXT_RE" in html
