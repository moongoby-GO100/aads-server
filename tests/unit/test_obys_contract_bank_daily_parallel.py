"""통장 원문 및 서명 원본 보호 회귀."""
import pytest
from fastapi import HTTPException

from app.services import yeoljeong_finance_service as svc


def test_masked_account_cannot_be_saved_as_original():
    with pytest.raises(HTTPException):
        svc._contract_account_number({"bank_account_number": "123-****-5678"})


def test_signed_bank_account_is_never_autofilled():
    contract = {"status": "signed", "tenant_id": "fixture", "bank_account_masked": "123-****-5678"}
    assert svc.contract_account_autofill_skip_reason(contract, None, tenant_id="fixture") == "status_signed"
    assert "bank_account_number" not in contract


@pytest.mark.parametrize(('wage_type', 'rate', 'expected'), [('daily', 100000, 200000), ('hourly', 12000, 180000)])
def test_attendance_daily_unique_dates_and_hourly_regression(monkeypatch, wage_type, rate, expected):
    employee = {"id": "join-fixture", "email": "fixture@example.test", "business_id": "biz-mia"}
    user = {"tenant_id": "fixture", "is_admin": True}
    monkeypatch.setattr(svc, "_tenant_id", lambda user: "fixture")
    monkeypatch.setattr(svc, "_is_admin", lambda user: True)
    monkeypatch.setattr(svc, "_find_employee_record", lambda *args, **kwargs: employee)
    monkeypatch.setattr(svc, "_require_business_for_tenant", lambda business, user: business)
    monkeypatch.setattr(svc, "_read_hr", lambda *args: [])
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *args, **kwargs: {"employment": {"wage_type": wage_type, "wage": rate}})
    rows = [
        {"id": "overnight", "work_date": "2026-11-01", "worked_minutes": 480, "status": "approved",
         "check_in_at": "2026-11-01T22:00:00+09:00", "check_out_at": "2026-11-02T06:00:00+09:00"},
        {"id": "same-day", "work_date": "2026-11-01", "worked_minutes": 60, "status": "approved"},
        {"id": "next-day", "work_date": "2026-11-02", "worked_minutes": 360, "status": "approved"},
    ]
    monkeypatch.setattr(svc, "_payroll_attendance_rows", lambda *args: rows)
    monkeypatch.setattr(svc, "_save_payroll_record", lambda request, user, server_fields: ({**request, **server_fields}, True))
    monkeypatch.setattr(svc, "_audit_payroll", lambda *args: True)
    result = svc.payroll_draft_from_attendance({"payroll_month": "2026-11", "employee_request_id": "join-fixture"}, user)
    assert result["gross_pay"] == expected
    assert result["attendance_basis"]["approved_days"] == 2
