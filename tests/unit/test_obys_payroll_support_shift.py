"""지원 근무 서버 계산 및 계약 없는 대표·가족 급여 회귀 검증."""
import pytest
from fastapi import HTTPException

from tests.unit.test_obys_payroll_payout_settlement import ADMIN, EMPLOYEE, MONTH, draft, isolated, svc
from app.services.yeoljeong_payroll_pdf import payroll_form_title, _pay_lines, payroll_totals


def shift(price=150000, quantity=1, unit="daily", day="01", **extra):
    return dict(code="support_shift", work_date=f"{MONTH}-{day}", support_branch="미아점",
                unit_type=unit, unit_price=price, quantity=quantity, amount=999999999, **extra)


def save_support(items=None, **extra):
    return draft(pay_items=items or [shift()], deduction_items=[], gross_pay=1, taxable_pay=0,
                 tax_withholding=0, insurance_deduction=0, **extra)


@pytest.mark.parametrize("item, expected", [(shift(120000),120000), (shift(10321,90,"hourly"),15482)])
def test_server_recalculates_tampered_amount(item, expected):
    row = save_support([item])
    assert row["gross_pay"] == expected
    assert row["taxable_pay"] == expected
    assert row["pay_items"][0]["amount"] == expected
    assert row["employment_tax_type"] == "daily_support"
    assert payroll_form_title(row) == "일용 지원 명세"
    assert "미아점" in _pay_lines(row, payroll_totals(row))[0][2]


@pytest.mark.parametrize("item", [shift(10000,60,"hourly"), shift(80000)])
def test_minimum_wage_warning_does_not_block(item):
    assert any("최저임금" in w for w in save_support([item])["payee_warnings"])


@pytest.mark.parametrize("amount,tax", [(150000,0),(180000,0),(200000,1485)])
def test_daily_withholding_small_payment(amount,tax):
    row = save_support([shift(amount)])
    assert row["tax_withholding"] == tax
    assert row["insurance_deduction"] == amount * 9 // 1000


def test_daily_tax_groups_same_date_and_small_tax_is_per_payment():
    row = save_support([shift(100000),shift(100000)])
    assert row["tax_withholding"] == 1485
    row = save_support([shift(180000),shift(180000,day="02")])
    assert row["tax_withholding"] == 1782


def test_monthly_base_support_remains_employment_income():
    row = save_support([dict(code="base",amount=3000000),shift(120000)])
    assert row["gross_pay"] == 3120000
    assert row.get("employment_tax_type") != "daily_support"


def test_owner_type_is_server_owned_and_withdrawal_has_no_deductions(monkeypatch):
    monkeypatch.setattr(svc,"get_settings",lambda user:{"settings":{"businesses":[{"id":"biz-1","entityType":"individual"}]}})
    row = save_support(payee_relation="owner",business_type="corporation",owner_withdrawal=False)
    assert row["owner_withdrawal"] is True
    assert row["business_type"] == "individual"
    assert row["tax_withholding"] == row["insurance_deduction"] == 0
    assert row["taxable_pay"] == 0
    assert row["contract_exempt"]


def test_corporation_owner_is_salary(monkeypatch):
    monkeypatch.setattr(svc,"get_settings",lambda user:{"settings":{"businesses":[{"id":"biz-1","entityType":"corporation"}]}})
    row = save_support(payee_relation="owner")
    assert not row["owner_withdrawal"]
    assert "임원 보수 규정·결의 필요" in row["payee_warnings"]


def test_family_evidence_warning_and_memo():
    assert "근무 증빙 필요" in save_support(payee_relation="family")["payee_warnings"]
    assert "근무 증빙 필요" not in save_support(payee_relation="family",work_memo="주말 지원 업무")["payee_warnings"]


def test_employee_cannot_assign_relation():
    with pytest.raises(HTTPException) as error:
        svc.save_payroll({"payee_relation":"owner"},EMPLOYEE)
    assert error.value.status_code == 403


@pytest.mark.parametrize("changes",[{"work_date":""},{"work_date":"2026-02-30"},{"work_date":"2026-09-01"},{"quantity":0},{"quantity":2},{"unit_price":-1}])
def test_invalid_support_is_rejected(changes):
    item=shift();item.update(changes)
    with pytest.raises(HTTPException):
        save_support([item])

