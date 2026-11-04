"""오비서 급여명세 초안 — 4대보험 법정 공제 자동 산정과 소득세 미입력 확정 차단.

파일 모드로만 돈다. 요율·상하한은 급여월 표이고, 표에 없는 달은 입력 필요 항목이 되어 확정을 막는다.
"""
import os
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, yeoljeong_ops_service
from app.services import yeoljeong_payroll_deductions as ded
from app.services import yeoljeong_payroll_pdf as pdf

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT,
         "current_membership": {"tenant_id": TENANT, "status": "active"}}
EMAIL = "member@example.com"


def floor10(value):
    return int(value) // 10 * 10


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")

    def disable_db(coroutine):
        close = getattr(coroutine, "close", None)
        if close:
            close()

    monkeypatch.setattr(svc, "_run_db", disable_db)
    clock = {"now": datetime(2026, 10, 20, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"]

    monkeypatch.setattr(svc, "_now_dt", fake_now)

    async def fake_notification(**kwargs):
        return {"id": "ntf", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    svc._write("employee_join_requests", [{
        "id": "join-1", "name": "가입 직원", "email": EMAIL, "tenant_id": TENANT, "business_id": "biz-1",
        "branch": "본점", "status": "approved",
    }])


def draft(month="2026-09", taxable=3_000_000, **extra):
    payload = {
        "employee_email": EMAIL, "employee_name": "가입 직원", "business_id": "biz-1", "branch": "본점",
        "payroll_month": month, "pay_date": f"{month}-25", "employment_tax_type": "four_insurance",
        "gross_pay": taxable, "taxable_pay": taxable, "non_tax_meal_allowance": 0,
        "pay_items": [{"code": "base", "label": "기본급", "amount": taxable}],
        "work_summary": {"work_days": 20},
    }
    payload.update(extra)
    return payload


def by_code(saved):
    return {item["code"]: item for item in saved["deduction_items"]}


def test_2026_09_rates_on_three_million():
    saved = svc.save_payroll(draft(), ADMIN)
    items = by_code(saved)
    assert items["national_pension"]["amount"] == 142_500
    assert items["health_insurance"]["amount"] == 107_850
    assert items["long_term_care"]["amount"] == floor10(107_850 * 0.1314) == 14_170
    assert items["employment_insurance"]["amount"] == 27_000
    assert items["income_tax"]["needs_input"] and items["local_income_tax"]["needs_input"]
    assert saved["insurance_deduction"] == 142_500 + 107_850 + 14_170 + 27_000
    assert saved["net_pay"] == 3_000_000 - saved["insurance_deduction"]
    assert saved["deduction_basis"]["taxable_pay"] == 3_000_000


def test_health_insurance_lower_and_upper_bounds_2026():
    low = ded.compute_statutory_items("2026-09", 250_000)
    assert low["health_insurance"]["amount"] == 10_080
    assert low["long_term_care"]["amount"] == floor10(10_080 * 1314 // 10_000) == 1_320
    assert "하한 10,080" in low["health_insurance"]["calc_note"]
    high = ded.compute_statutory_items("2026-09", 200_000_000)
    assert high["health_insurance"]["amount"] == 4_591_740
    assert high["long_term_care"]["amount"] == floor10(4_591_740 * 1314 // 10_000)
    edge = ded.compute_statutory_items("2026-09", 3_000_000)  # 범위 안은 그대로
    assert edge["health_insurance"]["amount"] == 107_850
    assert ded.compute_statutory_items("2026-09", 0)["health_insurance"]["amount"] == 0
    saved = svc.save_payroll(draft("2026-09", 250_000), ADMIN)
    assert by_code(saved)["health_insurance"]["amount"] == 10_080
    assert saved["deduction_basis"]["health_bounds"] == {"lower": 10_080, "upper": 4_591_740}


def test_duplicate_manual_same_code_items_are_preserved():
    saved = svc.save_payroll(draft(deduction_items=[
        {"code": "health_insurance", "label": "건강보험", "amount": 90_000, "source": "manual"},
        {"code": "health_insurance", "label": "건강보험 정산", "amount": 10_000, "source": "manual"}]), ADMIN)
    health = [i for i in saved["deduction_items"] if i["code"] == "health_insurance"]
    assert [(i["label"], i["amount"]) for i in health] == [("건강보험", 90_000), ("건강보험 정산", 10_000)]
    assert saved["insurance_deduction"] == 142_500 + 100_000 + 11_820 + 27_000
    assert saved["net_pay"] == 3_000_000 - saved["insurance_deduction"]
    again = svc.save_payroll(draft(id=saved["id"], deduction_items=saved["deduction_items"]), ADMIN)
    assert sorted(i["amount"] for i in again["deduction_items"] if i["code"] == "health_insurance") == [10_000, 90_000]
    assert again["insurance_deduction"] == saved["insurance_deduction"]


def test_duplicate_auto_copy_is_not_double_counted():
    saved = svc.save_payroll(draft(), ADMIN)
    dup = dict(by_code(saved)["health_insurance"])
    again = svc.save_payroll(draft(id=saved["id"], deduction_items=saved["deduction_items"] + [dup]), ADMIN)
    assert [i["amount"] for i in again["deduction_items"] if i["code"] == "health_insurance"] == [107_850]


def _auto_resave_with(extra_items, order="auto_first"):
    saved = svc.save_payroll(draft(), ADMIN)
    auto = [dict(i) for i in saved["deduction_items"] if i["code"] == "health_insurance"]
    assert [(i["amount"], i["source"]) for i in auto] == [(107_850, "auto")]
    rest = [i for i in saved["deduction_items"] if i["code"] != "health_insurance"]
    items = auto + extra_items if order == "auto_first" else extra_items + auto
    return svc.save_payroll(draft(id=saved["id"], deduction_items=items + rest), ADMIN)


def test_r1_auto_representative_then_manual_settlement_keeps_both():
    settlement = {"code": "health_insurance", "label": "건강보험 정산", "amount": 10_000, "source": "manual"}
    again = _auto_resave_with([settlement])
    health = [i for i in again["deduction_items"] if i["code"] == "health_insurance"]
    assert sum(i["amount"] for i in health) == 117_850
    assert [(i["amount"], i["source"]) for i in health] == [(107_850, "auto"), (10_000, "manual")]
    # 장기요양은 대표 건강보험 107,850 기준(정산 10,000 은 기준에 넣지 않는다): 107,850 × 13.14% = 14,172 → 14,170
    assert by_code(again)["long_term_care"]["amount"] == 14_170


def test_r2_manual_settlement_first_keeps_existing_behavior():
    settlement = {"code": "health_insurance", "label": "건강보험 정산", "amount": 10_000, "source": "manual"}
    again = _auto_resave_with([settlement], order="manual_first")
    health = [i for i in again["deduction_items"] if i["code"] == "health_insurance"]
    # 첫 항목이 수동이면 그 항목이 대표가 되고, 뒤의 변경 없는 자동 사본은 이중 계상 방지로 버려진다(기존 동작, F4).
    assert [(i["amount"], i["source"]) for i in health] == [(10_000, "manual")]
    # 장기요양은 수동 대표 10,000 기준: 10,000 × 13.14% = 1,314 → 1,310
    assert by_code(again)["long_term_care"]["amount"] == 1_310


def test_r3_two_unchanged_auto_copies_count_once():
    saved = svc.save_payroll(draft(), ADMIN)
    auto = [dict(i) for i in saved["deduction_items"] if i["code"] == "health_insurance"][0]
    rest = [i for i in saved["deduction_items"] if i["code"] != "health_insurance"]
    again = svc.save_payroll(draft(id=saved["id"], deduction_items=[auto, dict(auto), *rest]), ADMIN)
    assert [i["amount"] for i in again["deduction_items"] if i["code"] == "health_insurance"] == [107_850]


def test_review_duplicate_auto_item_with_edited_amount_is_preserved_as_manual():
    edited = {"code": "health_insurance", "label": "건강보험 정산", "amount": 12_000, "source": "auto"}
    for order in ("auto_first", "manual_first"):
        again = _auto_resave_with([edited], order=order)
        health = [i for i in again["deduction_items"] if i["code"] == "health_insurance"]
        assert sum(i["amount"] for i in health) == (119_850 if order == "auto_first" else 12_000)
        assert any(i["amount"] == 12_000 and i["source"] == "manual" for i in health)
    again = _auto_resave_with([edited])
    assert again["insurance_deduction"] == 142_500 + 119_850 + 14_170 + 27_000


def test_review_unconfirmed_manual_zero_insurance_in_unregistered_month_blocks_confirm():
    zeros = [{"code": code, "label": ded._LABELS[code], "amount": 0, "source": "manual"} for code in ded.INSURANCE_CODES]
    taxes = [{"code": code, "label": ded._LABELS[code], "amount": 0, "source": "manual", "confirmed": True}
             for code in ded.TAX_CODES]
    saved = svc.save_payroll(draft("2027-01", deduction_items=zeros + taxes), ADMIN)
    items = by_code(saved)
    for code in ded.INSURANCE_CODES:
        assert items[code]["needs_input"] is True
    assert set(ded.pending_input_codes(saved["deduction_items"])) == set(ded.INSURANCE_CODES)
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(saved["id"], ADMIN)
    assert exc.value.status_code == 409
    # 플래그가 사라진 저장본이 들어와도 미확인 수동 0원 보험료는 대기다. 자동 0원(과세급여 0)은 대기가 아니다.
    stripped = [{k: v for k, v in i.items() if k != "needs_input"} for i in saved["deduction_items"]]
    assert set(ded.pending_input_codes(stripped)) == set(ded.INSURANCE_CODES)
    assert "health_insurance" not in ded.pending_input_codes(ded.apply_statutory_deductions([], month="2026-09", taxable_pay=0)[0])
    confirmed = [dict(i, confirmed=True) if i["code"] in ded.INSURANCE_CODES else i for i in stripped]
    assert not ded.pending_input_codes(confirmed)


def test_pension_upper_and_lower_bounds_by_month():
    high = by_code(svc.save_payroll(draft("2026-09", 8_000_000), ADMIN))
    assert high["national_pension"]["amount"] == floor10(6_590_000 * 475 // 10_000)
    old = by_code(svc.save_payroll(draft("2026-03", 8_000_000, employee_email=EMAIL, id=""), ADMIN))
    assert old["national_pension"]["amount"] == floor10(6_370_000 * 475 // 10_000)
    low = ded.compute_statutory_items("2026-09", 300_000)
    assert low["national_pension"]["amount"] == floor10(410_000 * 475 // 10_000)


def test_2025_rates_and_unregistered_health_and_pension_bounds():
    items = ded.compute_statutory_items("2025-08", 3_000_000)
    assert items["national_pension"]["amount"] == 135_000
    # 2025 건강보험료 상·하한은 근거를 확인하지 못해 등록하지 않았다 — 요율만 믿고 계산하지 않고 입력 대기로 둔다.
    assert items["health_insurance"]["needs_input"] and "상하한 미등록" in items["health_insurance"]["calc_note"]
    assert items["long_term_care"]["needs_input"]
    assert items["employment_insurance"]["amount"] == 27_000
    early = ded.compute_statutory_items("2025-03", 3_000_000)  # 상하한 근거 없는 기간
    assert early["national_pension"]["needs_input"] and "상하한 미등록" in early["national_pension"]["calc_note"]


def test_unregistered_month_needs_input_and_blocks_confirm():
    saved = svc.save_payroll(draft("2027-01"), ADMIN)
    items = by_code(saved)
    for code in ded.INSURANCE_CODES:
        assert items[code]["needs_input"] is True
    assert "요율 미등록" in items["health_insurance"]["calc_note"]
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(saved["id"], ADMIN)
    assert exc.value.status_code == 409
    july = ded.compute_statutory_items("2027-07", 3_000_000)
    assert july["national_pension"]["needs_input"]


def test_income_tax_missing_blocks_confirm_until_admin_enters():
    saved = svc.save_payroll(draft(), ADMIN)
    with pytest.raises(HTTPException):
        svc.confirm_payroll(saved["id"], ADMIN)
    items = saved["deduction_items"]
    for item in items:
        if item["code"] == "income_tax":
            item.update(amount=50_000, source="manual")
        if item["code"] == "local_income_tax":
            item.update(amount=5_000, source="manual")
    again = svc.save_payroll({**draft(), "id": saved["id"], "deduction_items": items}, ADMIN)
    assert not ded.pending_input_codes(again["deduction_items"])
    assert svc.confirm_payroll(again["id"], ADMIN)["status"] == "confirmed"


def test_f1_taxable_change_recalculates_and_keeps_manual():
    saved = svc.save_payroll(draft(), ADMIN)
    items = saved["deduction_items"]
    for item in items:
        if item["code"] == "employment_insurance":
            item.update(amount=1_000, source="manual")
    payload = draft(taxable=4_000_000, id=saved["id"], deduction_items=items)
    payload["pay_items"] = [{"code": "base", "label": "기본급", "amount": 4_000_000}]
    again = svc.save_payroll(payload, ADMIN)
    got = by_code(again)
    assert got["national_pension"]["amount"] == 190_000
    assert got["health_insurance"]["amount"] == floor10(4_000_000 * 3595 // 100_000)
    assert got["employment_insurance"]["amount"] == 1_000  # 관리자 수정 보존
    assert again["deduction_basis"]["taxable_pay"] == 4_000_000


def test_f1_admin_edit_of_auto_item_without_source_change_is_preserved():
    saved = svc.save_payroll(draft(), ADMIN)
    items = saved["deduction_items"]
    for item in items:
        if item["code"] == "national_pension":
            item["amount"] = 100_000  # source 는 auto 로 남아 있지만 직전 자동값과 다르다
    again = svc.save_payroll(draft(id=saved["id"], deduction_items=items), ADMIN)
    assert by_code(again)["national_pension"]["amount"] == 100_000
    assert by_code(again)["national_pension"]["source"] == "manual"


def test_f3_other_deduction_does_not_skip_statutory_items():
    payload = draft(deduction_items=[{"code": "other", "label": "기타 공제", "amount": 1_000}])
    saved = svc.save_payroll(payload, ADMIN)
    assert set(by_code(saved)) == {*ded.REQUIRED_FOUR_INSURANCE_CODES, "other"}
    assert saved["other_deduction"] == 1_000
    totals_only = svc.save_payroll(draft(employee_email=EMAIL, other_deduction=1_000, id=""), ADMIN)
    assert set(by_code(totals_only)) == {*ded.REQUIRED_FOUR_INSURANCE_CODES, "other"}
    assert totals_only["other_deduction"] == 1_000


def test_f3_manual_same_code_kept_missing_codes_filled():
    saved = svc.save_payroll(draft(deduction_items=[
        {"code": "health_insurance", "label": "건강보험", "amount": 90_000, "source": "manual"}]), ADMIN)
    got = by_code(saved)
    assert got["health_insurance"]["amount"] == 90_000
    assert got["national_pension"]["amount"] == 142_500


def _set(items, code, **fields):
    for item in items:
        if item["code"] == code:
            item.update(fields)
    return items


def test_r1_flagless_zero_tax_is_pending_and_cannot_confirm():
    saved = svc.save_payroll(draft(), ADMIN)
    items = _set(saved["deduction_items"], "income_tax", amount=0, source="manual", needs_input=False)
    items = _set(items, "local_income_tax", amount=0, source="manual", needs_input=False)
    again = svc.save_payroll(draft(id=saved["id"], deduction_items=items), ADMIN)
    assert set(ded.pending_input_codes(again["deduction_items"])) == set(ded.TAX_CODES)
    assert all(by_code(again)[code]["needs_input"] for code in ded.TAX_CODES)
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(again["id"], ADMIN)
    assert exc.value.status_code == 409
    flagless = [dict(item, needs_input=False) for item in again["deduction_items"]]
    assert pdf.wage_statement_required_errors({**again, "deduction_items": flagless})  # 저장 우회 행도 확정 검증이 막는다


def test_r1_confirmed_zero_tax_is_allowed():
    saved = svc.save_payroll(draft(), ADMIN)
    items = saved["deduction_items"]
    for code in ded.TAX_CODES:
        _set(items, code, amount=0, source="manual", confirmed=True)
    again = svc.save_payroll(draft(id=saved["id"], deduction_items=items), ADMIN)
    assert not ded.pending_input_codes(again["deduction_items"])
    assert svc.confirm_payroll(again["id"], ADMIN)["status"] == "confirmed"


def test_r2_long_term_care_follows_applied_manual_health():
    saved = svc.save_payroll(draft(deduction_items=[
        {"code": "health_insurance", "label": "건강보험", "amount": 90_000, "source": "manual"}]), ADMIN)
    got = by_code(saved)
    assert got["long_term_care"]["amount"] == 11_820
    assert got["long_term_care"]["source"] == "auto"
    manual_ltc = svc.save_payroll(draft(deduction_items=[
        {"code": "health_insurance", "label": "건강보험", "amount": 90_000, "source": "manual"},
        {"code": "long_term_care", "label": "노인장기요양보험", "amount": 5_000, "source": "manual"}], employee_email=EMAIL, id=""), ADMIN)
    assert by_code(manual_ltc)["long_term_care"]["amount"] == 5_000


def test_review_manual_health_edited_again_recalculates_auto_long_term_care():
    first = svc.save_payroll(draft(deduction_items=[
        {"code": "health_insurance", "label": "건강보험", "amount": 90_000, "source": "manual"}]), ADMIN)
    assert by_code(first)["long_term_care"]["amount"] == 11_820
    items = _set(first["deduction_items"], "health_insurance", amount=100_000)
    second = svc.save_payroll(draft(id=first["id"], deduction_items=items), ADMIN)
    got = by_code(second)
    assert got["health_insurance"]["amount"] == 100_000 and got["health_insurance"]["source"] == "manual"
    assert got["long_term_care"]["amount"] == 13_140 and got["long_term_care"]["source"] == "auto"
    third = svc.save_payroll(draft(id=second["id"], deduction_items=second["deduction_items"]), ADMIN)
    assert by_code(third)["long_term_care"]["amount"] == 13_140
    assert third["deduction_basis"]["auto_amounts"]["long_term_care"] == 13_140


def test_r3_pension_floors_taxable_to_thousand_before_bounds():
    saved = svc.save_payroll(draft("2026-09", 3_000_999), ADMIN)
    item = by_code(saved)["national_pension"]
    assert item["amount"] == 142_500
    assert "천원 미만 절사" in item["calc_note"]
    assert ded.compute_statutory_items("2026-09", 409_999)["national_pension"]["amount"] == floor10(410_000 * 475 // 10_000)


def test_r4_lowering_gross_recalculates_before_deduction_cap():
    saved = svc.save_payroll(draft(), ADMIN)
    payload = draft(taxable=250_000, id=saved["id"], deduction_items=saved["deduction_items"],
                    tax_withholding=saved["tax_withholding"], insurance_deduction=saved["insurance_deduction"],
                    other_deduction=saved["other_deduction"])
    lowered = svc.save_payroll(payload, ADMIN)
    got = by_code(lowered)
    assert got["national_pension"]["amount"] == floor10(410_000 * 475 // 10_000)  # 하한 기준
    assert got["health_insurance"]["amount"] == 10_080  # 근로자 부담 하한 (250,000 × 3.595% = 8,980 은 하한 미만)
    assert got["long_term_care"]["amount"] == floor10(10_080 * 1314 // 10_000)
    assert lowered["insurance_deduction"] < 250_000 and lowered["net_pay"] == 250_000 - lowered["insurance_deduction"]


def test_r4_manual_items_over_gross_still_rejected():
    saved = svc.save_payroll(draft(), ADMIN)
    items = _set(saved["deduction_items"], "income_tax", amount=300_000, source="manual")
    payload = draft(taxable=250_000, id=saved["id"], deduction_items=items,
                    tax_withholding=300_000, insurance_deduction=saved["insurance_deduction"])
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(payload, ADMIN)
    assert exc.value.status_code == 400


def test_review_tax_total_pending_survives_resave_until_amount_edited_or_confirmed():
    first = svc.save_payroll(draft(tax_withholding=55_000), ADMIN)
    assert by_code(first)["income_tax"]["amount"] == 55_000 and by_code(first)["income_tax"]["needs_input"]
    items = _set(first["deduction_items"], "local_income_tax", amount=5_000, source="manual")
    second = svc.save_payroll(draft(id=first["id"], deduction_items=items), ADMIN)
    assert by_code(second)["income_tax"]["needs_input"] is True
    assert ded.pending_input_codes(second["deduction_items"]) == ["income_tax"]
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(second["id"], ADMIN)
    assert exc.value.status_code == 409
    stripped = [{k: v for k, v in item.items() if k != "needs_input"} for item in second["deduction_items"]]
    third = svc.save_payroll(draft(id=second["id"], deduction_items=stripped), ADMIN)
    assert by_code(third)["income_tax"]["needs_input"] is True  # 클라이언트가 플래그를 떨어뜨려도 금액이 그대로면 대기
    edited = _set(third["deduction_items"], "income_tax", amount=50_000)
    fourth = svc.save_payroll(draft(id=third["id"], deduction_items=edited), ADMIN)
    assert not ded.pending_input_codes(fourth["deduction_items"])
    assert svc.confirm_payroll(fourth["id"], ADMIN)["status"] == "confirmed"


def test_review_tax_total_confirmed_unchanged_amount_clears_pending():
    first = svc.save_payroll(draft(tax_withholding=55_000), ADMIN)
    items = _set(first["deduction_items"], "income_tax", confirmed=True)
    items = _set(items, "local_income_tax", amount=0, source="manual", confirmed=True)
    second = svc.save_payroll(draft(id=first["id"], deduction_items=items), ADMIN)
    assert by_code(second)["income_tax"]["amount"] == 55_000
    assert not ded.pending_input_codes(second["deduction_items"])


def test_f5_zero_tax_confirmed_survives_normalization_and_resave():
    saved = svc.save_payroll(draft(), ADMIN)
    items = saved["deduction_items"]
    for item in items:
        if item["code"] in ded.TAX_CODES:
            item.update(amount=0, needs_input=False, confirmed=True)
    first = svc.save_payroll(draft(id=saved["id"], deduction_items=items), ADMIN)
    assert not ded.pending_input_codes(first["deduction_items"])
    assert by_code(first)["income_tax"]["confirmed"] is True
    again = svc.save_payroll(draft(id=first["id"], deduction_items=first["deduction_items"]), ADMIN)
    assert not ded.pending_input_codes(again["deduction_items"])
    assert pdf.normalize_payroll_items(again)["deduction_items"] == pdf.normalize_payroll_items(
        {"deduction_items": pdf.normalize_payroll_items(again)["deduction_items"]})["deduction_items"]
    assert svc.confirm_payroll(again["id"], ADMIN)["status"] == "confirmed"


def _confirmable(saved):
    for item in saved["deduction_items"]:
        if item["code"] in ded.TAX_CODES:
            item.update(amount=0, confirmed=True)
    return {**saved, "deduction_items": saved["deduction_items"]}


@pytest.mark.parametrize("missing", ded.REQUIRED_FOUR_INSURANCE_CODES)
def test_f6_required_error_when_any_code_missing(missing):
    saved = svc.save_payroll(draft(), ADMIN)
    row = _confirmable(saved)
    assert pdf.wage_statement_required_errors(row) == []
    row["deduction_items"] = [i for i in row["deduction_items"] if i["code"] != missing]
    errors = pdf.wage_statement_required_errors(row)
    assert errors and "빠져" in errors[0]


def test_f6_no_deductions_at_all_cannot_confirm():
    row = {"employment_tax_type": "four_insurance", "pay_date": "2026-09-25", "gross_pay": 1000,
           "pay_items": [{"code": "base", "label": "기본급", "amount": 1000}]}
    assert pdf.wage_statement_required_errors(row)


def test_freelancer_and_locked_paths_unchanged():
    saved = svc.save_payroll({
        "employee_email": EMAIL, "employee_name": "가입 직원", "business_id": "biz-1", "payroll_month": "2026-09",
        "employment_tax_type": "freelancer_33", "gross_pay": 1_000_000, "tax_withholding": 33_000,
        "insurance_deduction": 0, "other_deduction": 0}, ADMIN)
    assert "deduction_basis" not in saved and not saved.get("deduction_items")
    assert saved["tax_withholding"] == 33_000


def test_income_tax_table_loader(tmp_path):
    good = tmp_path / "t.csv"
    good.write_text("salary_from,salary_to,family_1,family_2\n0,1000000,0,0\n1000000,1500000,1000,500\n", encoding="utf-8")
    table = ded.load_income_tax_table(str(good))
    assert ded.lookup_income_tax(table, 1_200_000, 2) == 500
    assert ded.lookup_income_tax(table, 9_000_000, 1) is None
    for name, text in {
        "gap": "salary_from,salary_to,family_1\n0,100,0\n200,300,0\n",
        "nohead": "a,b\n1,2\n",
        "neg": "salary_from,salary_to,family_1\n0,100,-5\n",
        "empty": "salary_from,salary_to,family_1\n",
        "rev": "salary_from,salary_to,family_1\n100,50,0\n",
    }.items():
        bad = tmp_path / f"{name}.csv"
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(ded.IncomeTaxTableError):
            ded.load_income_tax_table(str(bad))
