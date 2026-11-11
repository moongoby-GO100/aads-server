"""급여 공제 미리보기 API — 저장 없이 자동 계산·수동 보존·단시간 가입 제외·계산식 수정."""
import os

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from fastapi import FastAPI
from fastapi.testclient import TestClient

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT,
         "current_membership": {"tenant_id": TENANT, "status": "active"}}
URL = "/yeoljeong-finance/payroll/deductions/preview"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import obys_finance as api

    svc = api.svc
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    writes = []
    monkeypatch.setattr(svc, "_write", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(svc, "_run_db", lambda coroutine: getattr(coroutine, "close", lambda: None)())
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: ADMIN
    # 월 소정근로시간은 계약에서만 읽는다 — 계약 조회를 합성하고 시간은 client.contract_hours 로 정한다.
    monkeypatch.setattr(svc, "_find_employee_record", lambda *a, **k: {"email": "emp@example.test", "id": "e1"})
    monkeypatch.setattr(svc, "_read_hr", lambda *a, **k: [])
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: {"employment": {}})
    c = TestClient(app)
    c.writes = writes
    c.contract_hours = None
    monkeypatch.setattr(svc, "_monthly_scheduled_hours", lambda employment: c.contract_hours)
    return c


def _post(client, contract_hours=None, **body):
    """contract_hours: 계약에서 읽히는 월 소정근로시간(합성). 요청 본문에는 직원만 싣는다."""
    client.contract_hours = contract_hours
    body = {"payroll_month": "2026-10", "taxable_pay": 2_000_000, "deduction_items": [],
            "employee_email": "emp@example.test", **body}
    return client.post(URL, json=body)


def _by_code(response):
    assert response.status_code == 200, response.text
    return {row["code"]: row for row in response.json()["deduction_items"]}


def test_a_auto_values_with_calc_notes_and_no_db_write(client):
    r = _post(client, contract_hours=174)
    rows = _by_code(r)
    for code in ("national_pension", "health_insurance", "long_term_care", "employment_insurance", "income_tax", "local_income_tax"):
        assert rows[code]["source"] == "auto" and rows[code]["calc_note"]
    assert rows["national_pension"]["amount"] > 0 and rows["health_insurance"]["amount"] > 0
    body = r.json()
    assert body["tax_withholding"] == rows["income_tax"]["amount"] + rows["local_income_tax"]["amount"]
    assert body["insurance_deduction"] == sum(rows[c]["amount"] for c in ("national_pension", "health_insurance", "long_term_care", "employment_insurance"))
    assert body["total_deductions"] == body["tax_withholding"] + body["insurance_deduction"] + body["other_deduction"]
    assert rows["national_pension"]["formula"]["base"] == 2_000_000
    assert client.writes == []


def test_b_short_time_excludes_pension_health_ltc_and_employment_needs_input(client):
    rows = _by_code(_post(client, taxable_pay=570_000, contract_hours=45.6))
    for code in ("national_pension", "health_insurance", "long_term_care"):
        assert rows[code]["amount"] == 0 and "가입 제외" in rows[code]["calc_note"] and "formula" not in rows[code]
    assert rows["employment_insurance"]["needs_input"] is True


def test_c_manual_health_kept_and_ltc_follows(client):
    rows = _by_code(_post(client, contract_hours=174, deduction_items=[
        {"code": "health_insurance", "label": "건강보험", "amount": 50_000, "source": "manual"}]))
    assert rows["health_insurance"]["amount"] == 50_000 and rows["health_insurance"]["source"] == "manual"
    ltc = rows["long_term_care"]
    assert ltc["source"] == "auto" and ltc["amount"] > 0 and "50,000" in ltc["calc_note"]


def test_c2_short_time_manual_health_kept_ltc_zero(client):
    rows = _by_code(_post(client, contract_hours=45.6, deduction_items=[
        {"code": "health_insurance", "label": "건강보험", "amount": 10_080, "source": "manual"}]))
    assert rows["health_insurance"]["amount"] == 10_080 and rows["health_insurance"]["source"] == "manual"
    assert rows["long_term_care"]["amount"] == 0 and "가입 제외" in rows["long_term_care"]["calc_note"]


def test_d_manual_zero_confirmed_has_no_pending_and_totals_reflect(client):
    r = _post(client, contract_hours=174, deduction_items=[
        {"code": "national_pension", "label": "국민연금", "amount": 0, "source": "manual", "confirmed": True}])
    rows = _by_code(r)
    assert rows["national_pension"]["needs_input"] is False and "national_pension" not in r.json()["pending_codes"]
    assert r.json()["insurance_deduction"] == sum(rows[c]["amount"] for c in ("health_insurance", "long_term_care", "employment_insurance"))


def test_unconfirmed_manual_zero_is_pending(client):
    r = _post(client, contract_hours=174, deduction_items=[
        {"code": "national_pension", "label": "국민연금", "amount": 0, "source": "manual"}])
    assert "national_pension" in r.json()["pending_codes"]


def test_formula_edit_recalculates_and_becomes_manual(client):
    rows = _by_code(_post(client, contract_hours=174, deduction_items=[
        {"code": "health_insurance", "label": "건강보험", "amount": 1, "source": "auto",
         "formula": {"base": 1_000_000, "rate_pct": 3.5, "edited": True}}]))
    assert rows["health_insurance"]["amount"] == 35_000 and rows["health_insurance"]["source"] == "manual"
    assert "기준금액 1,000,000원" in rows["health_insurance"]["calc_note"]


def test_auto_rows_with_stale_prior_amounts_stay_auto_when_equal_to_preview(client):
    first = _by_code(_post(client, contract_hours=174))
    again = _by_code(_post(client, contract_hours=174, deduction_items=list(first.values())))
    assert all(row["source"] == "auto" for row in again.values())
    assert {c: r["amount"] for c, r in again.items()} == {c: r["amount"] for c, r in first.items()}


def test_manual_calc_note_is_preserved(client):
    rows = _by_code(_post(client, contract_hours=174, deduction_items=[
        {"code": "national_pension", "label": "국민연금", "amount": 90_000, "source": "manual", "calc_note": "관리자 메모"}]))
    assert rows["national_pension"]["calc_note"] == "관리자 메모"


@pytest.mark.parametrize("body", [
    {"payroll_month": "2026-13"}, {"taxable_pay": "abc"}, {"deduction_items": "bad"},
    {"deduction_items": [{"code": "health_insurance", "label": "건강보험", "amount": 1,
                          "formula": {"base": "-1", "rate_pct": 3, "edited": True}}]},
])
def test_f_invalid_input_returns_http_400(client, body):
    assert _post(client, **body).status_code == 400


def test_non_admin_is_denied(client):
    from app.api import obys_finance as api

    client.app.dependency_overrides[api.get_current_user] = lambda: {**ADMIN, "is_admin": False}
    assert _post(client).status_code in (401, 403)


def test_e_payload_source_confirmed_calc_note_survive_normalization():
    from app.services.yeoljeong_payroll_pdf import normalize_payroll_items

    items = normalize_payroll_items({"deduction_items": [
        {"code": "national_pension", "label": "국민연금", "amount": 0, "source": "manual", "confirmed": True, "calc_note": "메모"}]})["deduction_items"]
    assert items[0]["source"] == "manual" and items[0]["confirmed"] is True and items[0]["calc_note"] == "메모"


@pytest.mark.parametrize("text,expected", [("주 10시간 30분", 10.5), ("주 10.5시간", 10.5), ("주10시간", 10.0), ("주 10시간", 10.0)])
def test_g_weekly_hours_parsing(text, expected):
    from app.services.yeoljeong_finance_service import _monthly_scheduled_hours

    got = _monthly_scheduled_hours({"weekly_hours": text})
    assert got == pytest.approx(expected * 365 / 7 / 12)


@pytest.mark.parametrize("text", ["", "주 시간", "미정"])
def test_g_unreadable_weekly_hours_is_none(text):
    from app.services.yeoljeong_finance_service import _monthly_scheduled_hours

    assert _monthly_scheduled_hours({"weekly_hours": text}) is None


def test_auto_tax_rows_follow_changed_taxable_pay(client):
    """리뷰 P1-1: 옛 과세급여의 자동 소득세·지방소득세(0원 아님)도 지급액이 바뀌면 새 값으로 갱신된다."""
    old = _by_code(_post(client, taxable_pay=3_000_000, contract_hours=174))
    assert old["income_tax"]["amount"] > 0
    new = _by_code(_post(client, taxable_pay=5_000_000, contract_hours=174, deduction_items=list(old.values())))
    fresh = _by_code(_post(client, taxable_pay=5_000_000, contract_hours=174))
    for code in ("income_tax", "local_income_tax"):
        assert new[code]["source"] == "auto" and new[code]["amount"] == fresh[code]["amount"] != old[code]["amount"]


def test_manual_tax_revert_to_auto_recalculates(client):
    """수동 세금 행을 source=auto 로 되돌리면(되돌리기 버튼) 자동값으로 돌아온다."""
    fresh = _by_code(_post(client, contract_hours=174))
    reverted = _by_code(_post(client, contract_hours=174, deduction_items=[
        {"code": "income_tax", "label": "소득세", "amount": 12_340, "source": "auto"}]))
    assert reverted["income_tax"]["amount"] == fresh["income_tax"]["amount"] and reverted["income_tax"]["source"] == "auto"
    manual = _by_code(_post(client, contract_hours=174, deduction_items=[
        {"code": "income_tax", "label": "소득세", "amount": 12_340, "source": "manual"}]))
    assert manual["income_tax"]["amount"] == 12_340


def test_formula_edit_keeps_admin_written_note(client):
    """리뷰 P1-3: 관리자가 직접 쓴 메모(note_manual)는 계산식 수정 때 자동 문구로 덮어쓰지 않는다."""
    item = {"code": "health_insurance", "label": "건강보험", "amount": 1, "source": "manual",
            "formula": {"base": 1_000_000, "rate_pct": 3.5, "edited": True}}
    kept = _by_code(_post(client, contract_hours=174, deduction_items=[
        {**item, "calc_note": "노무사 확인 후 수정", "note_manual": True}]))
    assert kept["health_insurance"]["amount"] == 35_000 and kept["health_insurance"]["calc_note"] == "노무사 확인 후 수정"
    # 표시가 없으면 접두사가 비슷해도 자동 문구로 다시 쓴다(내용으로 추측하지 않는다).
    auto_note = _by_code(_post(client, contract_hours=174, deduction_items=[{**item, "calc_note": "기준금액 1원"}]))
    assert "기준금액 1,000,000원" in auto_note["health_insurance"]["calc_note"]


def test_family_changes_income_tax(client):
    one = _by_code(_post(client, contract_hours=174, family=1))["income_tax"]["amount"]
    four = _by_code(_post(client, contract_hours=174, family=4))["income_tax"]["amount"]
    assert four < one


def test_empty_deduction_items_creates_auto_rows(client):
    rows = _by_code(_post(client, taxable_pay=2_500_000, contract_hours=174))
    assert rows["national_pension"]["amount"] > 0


def test_preview_uses_contract_hours_not_work_minutes(client, monkeypatch):
    """화면은 monthly_scheduled_hours 를 보내지 않는다. 계약 174시간 직원은 근로시간(분)을 45시간으로 바꿔도 가입 제외가 아니다."""
    svc = client.app.router.routes and __import__("app.api.obys_finance", fromlist=["svc"]).svc
    employee = {"id": "emp-1", "email": "worker@example.com"}
    monkeypatch.setattr(svc, "_find_employee_record", lambda *a, **k: employee)
    monkeypatch.setattr(svc, "_read_hr", lambda *a, **k: [])
    monkeypatch.setattr(svc, "_record_business_id", lambda *a, **k: "b1")
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: {"employment": {"weekly_hours": "40"}})
    base = {"employee_email": "worker@example.com", "employee_request_id": "emp-1", "work_minutes": 2700}
    from_contract = _by_code(_post(client, **base))
    assert from_contract["national_pension"]["amount"] > 0 and from_contract["health_insurance"]["amount"] > 0
    expected = svc._monthly_scheduled_hours({"weekly_hours": "40"})
    explicit = _by_code(_post(client, monthly_scheduled_hours=expected))
    for code in ("national_pension", "health_insurance", "long_term_care", "employment_insurance"):
        assert from_contract[code]["amount"] == explicit[code]["amount"]


@pytest.mark.parametrize("sent", [45, 0, "x", -5])
def test_request_monthly_scheduled_hours_is_ignored_contract_only(client, sent):
    """계약 174시간이면 요청에 45시간을 보내도 가입 제외가 아니다(저장 경로와 같은 판정)."""
    r = _post(client, contract_hours=174, monthly_scheduled_hours=sent)
    assert r.status_code == 200, r.text
    rows = _by_code(r)
    assert r.json()["short_time_exclusion"] is False and r.json()["monthly_scheduled_hours"] == 174
    assert rows["national_pension"]["amount"] > 0 and "가입 제외" not in rows["national_pension"]["calc_note"]


def test_request_hours_cannot_exclude_without_contract(client):
    r = _post(client, contract_hours=None, monthly_scheduled_hours=10)
    assert r.json()["short_time_exclusion"] is False and r.json()["monthly_scheduled_hours"] is None
