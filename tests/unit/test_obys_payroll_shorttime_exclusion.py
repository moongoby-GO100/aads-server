"""월 소정근로 60시간 미만 단시간근로자 — 국민연금·건강보험·장기요양 자동 0원, 고용보험 확인 대기."""
import os

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import yeoljeong_payroll_deductions as ded

svc = api.svc
MONTH = "2026-10"
EXCLUDED = ("national_pension", "health_insurance", "long_term_care")


def weekly_to_monthly(weekly):
    return weekly * 365 / 7 / 12


def by_code(items):
    return {item["code"]: item for item in items}


def apply(hours, taxable=87500, items=None, previous_basis=None):
    return ded.apply_statutory_deductions(
        items, month=MONTH, taxable_pay=taxable, previous_basis=previous_basis, monthly_scheduled_hours=hours,
    )


def test_short_time_excludes_three_and_holds_employment_insurance():
    hours = weekly_to_monthly(10.5)
    assert round(hours, 1) == 45.6
    items, basis = apply(hours)
    got = by_code(items)
    for code in EXCLUDED:
        assert got[code]["amount"] == 0
        assert got[code]["source"] == "auto"
        assert not got[code].get("needs_input")
        assert "60시간 미만" in got[code]["calc_note"] and "가입 제외" in got[code]["calc_note"]
        assert "시행령" in got[code]["calc_note"]
    ei = got["employment_insurance"]
    assert ei["amount"] == 780
    assert ei["needs_input"] is True
    assert "고용보험법 시행령 제3조 단서" in ei["calc_note"]
    assert basis["short_time_exclusion"]["threshold"] == 60
    assert basis["short_time_exclusion"]["monthly_scheduled_hours"] == round(hours, 2)
    assert "employment_insurance" in ded.pending_input_codes(items)


def test_fifteen_hours_week_matches_current_behavior():
    hours = weekly_to_monthly(15)
    assert round(hours, 1) == 65.2
    assert apply(hours) == apply(None)


def test_none_is_identical_to_legacy_call():
    legacy = ded.apply_statutory_deductions(None, month=MONTH, taxable_pay=87500)
    assert apply(None) == legacy
    assert ded.compute_statutory_items(MONTH, 87500) == ded.compute_statutory_items(MONTH, 87500, None, None)
    assert "short_time_exclusion" not in legacy[1]
    assert by_code(legacy[0])["national_pension"]["amount"] > 0


def test_manual_items_are_preserved():
    manual = {"code": "national_pension", "label": "국민연금", "amount": 12340, "source": "manual"}
    confirmed_ei = {"code": "employment_insurance", "label": "고용보험", "amount": 780, "source": "auto", "confirmed": True}
    items, _ = apply(weekly_to_monthly(10.5), items=[manual, confirmed_ei])
    got = by_code(items)
    assert got["national_pension"]["amount"] == 12340
    assert got["national_pension"]["source"] == "manual"
    assert got["employment_insurance"]["confirmed"] is True
    assert not got["employment_insurance"].get("needs_input")
    assert got["health_insurance"]["amount"] == 0


def test_manual_health_does_not_revive_auto_long_term_care():
    manual = {"code": "health_insurance", "label": "건강보험", "amount": 10080, "source": "manual"}
    got = by_code(apply(weekly_to_monthly(10.5), items=[manual])[0])
    assert got["health_insurance"]["amount"] == 10080
    assert got["health_insurance"]["source"] == "manual"
    assert got["long_term_care"]["amount"] == 0
    assert got["long_term_care"]["source"] == "auto"
    assert "가입 제외" in got["long_term_care"]["calc_note"]


def test_manual_long_term_care_is_preserved():
    manual = {"code": "long_term_care", "label": "장기요양", "amount": 1320, "source": "manual"}
    got = by_code(apply(weekly_to_monthly(10.5), items=[manual])[0])
    assert got["long_term_care"]["amount"] == 1320
    assert got["long_term_care"]["source"] == "manual"


def test_employment_note_kept_when_rate_unregistered():
    items = ded.compute_statutory_items("2099-01", 87500, None, weekly_to_monthly(10.5))
    ei = items["employment_insurance"]
    assert ei["needs_input"] is True
    assert "3개월 이상 계속 근로" in ei["calc_note"]
    assert "고용보험법 시행령 제3조" in ei["calc_note"]


def test_previous_auto_values_are_replaced_when_hours_become_short():
    items, basis = apply(None)
    assert by_code(items)["national_pension"]["amount"] > 0
    again, _ = apply(weekly_to_monthly(10.5), items=items, previous_basis=basis)
    assert by_code(again)["national_pension"]["amount"] == 0


@pytest.mark.parametrize("hours,short", [(59.9, True), (60.0, False), (0.0, True), (None, False)])
def test_boundary(hours, short):
    got = by_code(apply(hours)[0])
    assert (got["national_pension"]["amount"] == 0) is short
    assert bool(got["employment_insurance"].get("needs_input")) is short


def test_income_taxes_unchanged_by_short_time():
    a = ded.compute_statutory_items(MONTH, 1_500_000)
    b = ded.compute_statutory_items(MONTH, 1_500_000, None, 45.6)
    for code in ded.TAX_CODES:
        assert a[code] == b[code]


@pytest.mark.parametrize("text,expected", [
    ("주 10시간 30분", 10.5 * 365 / 7 / 12), ("주 15시간", 15 * 365 / 7 / 12), ("", None), ("미정", None),
])
def test_contract_weekly_hours_parsing(text, expected):
    got = svc._monthly_scheduled_hours({"weekly_hours": text})
    assert (got is None) if expected is None else got == pytest.approx(expected)
    assert svc._monthly_scheduled_hours(None) is None
