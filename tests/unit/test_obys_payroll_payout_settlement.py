"""오비서 급여 — 기준(신고)명세 고정 + 실제 지급액 기록 + 차액 추가지급/다음 달 정산·회수 흐름.

파일 모드로만 돈다. 기준명세 금액은 어떤 처리 뒤에도 바뀌지 않아야 하고,
다음 달 정산 항목은 과세 기준(원천세·4대보험 계산 기준)에 들어가지 않아야 한다.
정산 상태는 저장하지 않고 버전 체인(root_id) 단위 원장으로 계산한다 — 원 명세에 링크를 쓰지 않는다.
"""
import os
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, yeoljeong_ops_service

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
MONTH, NEXT = "2026-10", "2026-11"
AMOUNT_FIELDS = ("gross_pay", "taxable_pay", "tax_withholding", "insurance_deduction", "other_deduction", "net_pay")


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
    clock = {"now": datetime(2026, 11, 6, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"]

    monkeypatch.setattr(svc, "_now_dt", fake_now)

    async def fake_notification(**kwargs):
        return {"id": "ntf", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    svc._write("employee_join_requests", [{
        "id": "join-1", "name": "가입 직원", "email": EMPLOYEE["email"], "tenant_id": TENANT,
        "business_id": "biz-1", "branch": "본점", "status": "approved",
    }])


def draft(month=MONTH, gross=3000000, **extra):
    payload = {
        "employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-1", "branch": "본점",
        "payroll_month": month, "pay_date": f"{month}-10", "gross_pay": gross, "tax_withholding": 100000,
        "insurance_deduction": 200000, "other_deduction": 0,
        "pay_items": [{"code": "base", "label": "기본급", "amount": gross, "calc_note": "월 기본급"}],
        "deduction_items": [{"code": "income_tax", "label": "소득세", "amount": 100000},
                            {"code": "national_pension", "label": "국민연금", "amount": 200000}],
    }
    payload.update(extra)
    return svc.save_payroll(payload, ADMIN)


def confirmed(**kwargs):
    saved = draft(**kwargs)
    return svc.confirm_payroll(saved["id"], ADMIN)


def paid(amount, **kwargs):
    row = confirmed(**kwargs)
    return svc.pay_payroll(row["id"], ADMIN, paid_amount=amount, paid_date="2026-10-10", paid_source="bank")


def base_numbers(row):
    return {field: row.get(field) for field in AMOUNT_FIELDS}


def stored(statement_id):
    return next(row for row in svc._read_hr("payroll_statements", ADMIN) if row["id"] == statement_id)


# --- A. 지급 기록 ----------------------------------------------------------------


def test_pay_without_input_records_full_net_pay():
    row = confirmed()
    done = svc.pay_payroll(row["id"], ADMIN)
    assert done["status"] == "paid"
    assert done["payout"]["amount"] == 2700000 and done["payout"]["source"] == "manual"
    assert done["payout"]["recorded_by"] == ADMIN["email"] and done["payout"]["date"]
    assert svc.payroll_payout_compare(done)["diff_amount"] == 0


def test_pay_with_input_keeps_statement_amounts_and_validates():
    row = confirmed()
    for bad in ({"paid_amount": -1}, {"paid_amount": 1.5}, {"paid_amount": "abc"}, {"paid_source": "cash"}, {"paid_date": "2026-13-40"}):
        with pytest.raises(HTTPException) as exc:
            svc.pay_payroll(row["id"], ADMIN, **bad)
        assert exc.value.status_code == 400
    done = svc.pay_payroll(row["id"], ADMIN, paid_amount=2619120, paid_date="2026-10-10", paid_source="sheet")
    assert base_numbers(done) == base_numbers(row)
    assert done["payout"] == {**done["payout"], "amount": 2619120, "date": "2026-10-10", "source": "sheet"}


def test_payout_correction_changes_only_payout_and_is_audited():
    done = paid(2619120)
    fixed = svc.record_payroll_payout(done["id"], ADMIN, 2700000, "2026-10-11", "manual")
    assert base_numbers(fixed) == base_numbers(done) and fixed["status"] == "paid"
    assert fixed["payout"]["amount"] == 2700000
    actions = [r["action"] for r in svc._read_file_rows(svc.EMPLOYMENT_AUDIT_LOG)]
    assert "payroll.payout_corrected" in actions
    with pytest.raises(HTTPException) as exc:
        svc.record_payroll_payout(confirmed(payroll_month="2026-09")["id"], ADMIN, 1)
    assert exc.value.status_code == 409


def test_compare_priority_payout_then_bank_then_raw_net():
    row = {"net_pay": 1000, "raw_net": 900, "bank_paid_amount": 950, "payout": {"amount": 800}}
    assert svc.payroll_payout_compare(row) == {"basis": "payout", "compared_amount": 800, "base_net": 1000, "diff_amount": -200}
    del row["payout"]
    assert svc.payroll_payout_compare(row)["basis"] == "bank"
    del row["bank_paid_amount"]
    assert svc.payroll_payout_compare(row)["diff_amount"] == -100
    del row["raw_net"]
    assert svc.payroll_payout_compare(row) is None


# --- B. 차액 처리 ----------------------------------------------------------------


def test_resolution_rejected_on_draft_and_allows_confirmed_with_sheet_value():
    saved = draft()
    with pytest.raises(HTTPException) as exc:
        svc.set_payroll_payout_diff_resolution(saved["id"], ADMIN, {"method": "extra_pay"})
    assert exc.value.status_code == 409 and "기준명세를 먼저 확정" in exc.value.detail


def test_resolution_sign_mismatch_zero_and_unknown_are_rejected_with_korean_detail():
    under = paid(2619120)
    for method in ("next_month_recover", "direct_refund", "accept", "match_paid"):
        with pytest.raises(HTTPException) as exc:
            svc.set_payroll_payout_diff_resolution(under["id"], ADMIN, {"method": method})
        assert exc.value.status_code == 400 and any("가" <= ch <= "힣" for ch in exc.value.detail)
    exact = paid(2700000, payroll_month="2026-09")
    with pytest.raises(HTTPException) as exc:
        svc.set_payroll_payout_diff_resolution(exact["id"], ADMIN, {"method": "extra_pay"})
    assert exc.value.status_code == 400
    over = paid(2800000, payroll_month="2026-08")
    with pytest.raises(HTTPException) as exc:
        svc.set_payroll_payout_diff_resolution(over["id"], ADMIN, {"method": "extra_pay"})
    assert exc.value.status_code == 400


def test_resolution_records_fields_keeps_statement_and_can_be_cancelled():
    under = paid(2619120)
    before = base_numbers(under)
    done = svc.set_payroll_payout_diff_resolution(under["id"], ADMIN, {"method": "extra_pay", "memo": "추가 이체 예정", "scheduled_date": "2026-11-10"})
    res = done["payout_diff_resolution"]
    assert res["method"] == "extra_pay" and res["diff_amount"] == -80880 and res["basis"] == "payout"
    assert res["compared_amount"] == 2619120 and res["base_net"] == 2700000 and res["decided_by"] == ADMIN["email"]
    assert base_numbers(done) == before and done["status"] == "paid"
    completed = svc.complete_payroll_payout_diff_resolution(under["id"], ADMIN, "2026-11-11")
    assert completed["payout_diff_resolution"]["completed_date"] == "2026-11-11"
    with pytest.raises(HTTPException) as exc:
        svc.cancel_payroll_payout_diff_resolution(under["id"], ADMIN)
    assert exc.value.status_code == 409
    other = paid(2619120, payroll_month="2026-09")
    svc.set_payroll_payout_diff_resolution(other["id"], ADMIN, {"method": "extra_pay"})
    cancelled = svc.cancel_payroll_payout_diff_resolution(other["id"], ADMIN)
    assert "payout_diff_resolution" not in cancelled and base_numbers(cancelled) == base_numbers(other)
    with pytest.raises(HTTPException):
        svc.cancel_payroll_payout_diff_resolution(other["id"], ADMIN)


def test_resolution_applies_to_confirmed_with_sheet_amount_and_blocks_payout_drift():
    row = confirmed(raw_net=2619120)
    done = svc.set_payroll_payout_diff_resolution(row["id"], ADMIN, {"method": "next_month_pay"})
    assert done["payout_diff_resolution"]["basis"] == "sheet" and done["status"] == "confirmed"
    with pytest.raises(HTTPException) as exc:
        svc.pay_payroll(row["id"], ADMIN, paid_amount=2700000)
    assert exc.value.status_code == 409


def test_non_admin_and_other_tenant_are_rejected():
    under = paid(2619120)
    other_admin = {"email": "boss@other.example.com", "is_admin": True, "tenant_id": "2a1b3c4d-0000-4000-8000-000000000001",
                   "current_membership": {"tenant_id": "2a1b3c4d-0000-4000-8000-000000000001", "status": "active"}}
    for user, code in ((EMPLOYEE, 403), (other_admin, 404)):
        for call in (
            lambda u=user: svc.set_payroll_payout_diff_resolution(under["id"], u, {"method": "extra_pay"}),
            lambda u=user: svc.record_payroll_payout(under["id"], u, 1),
            lambda u=user: svc.pay_payroll(under["id"], u),
            lambda u=user: svc.cancel_payroll_payout_diff_resolution(under["id"], u),
            lambda u=user: svc.complete_payroll_payout_diff_resolution(under["id"], u),
        ):
            with pytest.raises(HTTPException) as exc:
                call()
            assert exc.value.status_code == code


def test_http_routes_exist_and_employee_view_hides_resolution():
    under = paid(2619120)
    client = TestClient(_app(ADMIN))
    r = client.post(f"/yeoljeong-finance/payroll/{under['id']}/payout-diff-resolution", json={"method": "extra_pay"})
    assert r.status_code == 200 and r.json()["statement"]["payout_diff_resolution"]["method"] == "extra_pay"
    assert client.post(f"/yeoljeong-finance/payroll/{under['id']}/payout", json={"paid_amount": 2700000}).status_code == 409  # 차액 처리 뒤에는 지급액 정정 불가
    assert client.post(f"/yeoljeong-finance/payroll/{under['id']}/payout-diff-resolution/complete", json={"date": "2026-11-12"}).status_code == 200
    assert client.delete(f"/yeoljeong-finance/payroll/{under['id']}/payout-diff-resolution").status_code == 409
    mine = svc.list_payroll(EMPLOYEE, MONTH)
    assert mine and all("payout_diff_resolution" not in row for row in mine)


def _app(user):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: user
    return app


# --- C. 다음 달 정산 -------------------------------------------------------------


def _defaults(month=NEXT):
    return svc.payroll_defaults(EMPLOYEE["email"], month, ADMIN)


def _state(row_id):
    rows = svc._read_hr("payroll_statements", ADMIN)
    return svc._row_settlement_state(rows, next(row for row in rows if row["id"] == row_id))


def _left(row_id):
    """초안 청구까지 반영했을 때 남는 금액. pending(I3)은 사실만 빼므로 청구는 따로 뺀다."""
    state = _state(row_id)
    return state["pending_amount"] - state["claimed_amount"]


def _next_payload(items=None, gross=3000000, **extra):
    items = items or {"pay_items": [], "deduction_items": []}
    pay = [{"code": "base", "label": "기본급", "amount": gross, "calc_note": "월 기본급"}, *items["pay_items"]]
    total = sum(i["amount"] for i in pay)
    payload = {
        "employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-1", "branch": "본점",
        "payroll_month": NEXT, "pay_date": f"{NEXT}-10", "gross_pay": total, "taxable_pay": gross, "non_tax_meal_allowance": 0,
        "pay_items": pay, "deduction_items": list(items["deduction_items"]),
        "tax_withholding": 0, "insurance_deduction": 0, "employment_tax_type": "other",
        "other_deduction": sum(i["amount"] for i in items["deduction_items"]),
    }
    payload.update(extra)
    return payload


def _resolved(method="next_month_pay", amount=2619120, **kwargs):
    row = paid(amount, **kwargs)
    return svc.set_payroll_payout_diff_resolution(row["id"], ADMIN, {"method": method})


def _settlement_items(row):
    return [item for field in ("pay_items", "deduction_items") for item in row.get(field) or [] if item.get("settlement_of_root_id")]


def test_next_month_pay_applied_once_without_touching_taxable_base():
    under = _resolved()
    items = _defaults()["settlement_items"]
    assert [i["amount"] for i in items["pay_items"]] == [80880] and items["deduction_items"] == []
    item = items["pay_items"][0]
    assert item["label"] == "전월 미지급분 지급" and item["code"] == "prior_month_unpaid_pay"
    assert item["source"] == "settlement" and item["settlement_of_root_id"] == under["root_id"]

    plain = svc.save_payroll(_next_payload(employment_tax_type="four_insurance", settlement_opt_out=[under["root_id"]]), ADMIN)
    assert not _settlement_items(plain) and plain["settlement_opt_out"] == [under["root_id"]]
    svc.delete_payroll(plain["id"], ADMIN)
    saved = svc.save_payroll(_next_payload(items, employment_tax_type="four_insurance"), ADMIN)
    assert saved["gross_pay"] == 3080880 and saved["taxable_pay"] == 3000000
    for field in ("tax_withholding", "insurance_deduction"):
        assert saved[field] == plain[field], field
    assert base_numbers(stored(under["id"])) == base_numbers(under)
    state = _state(under["id"])
    assert state["pending_amount"] == -80880 and state["settled_amount"] == 0 and state["claimed_amount"] == -80880
    assert state["settled_in"] == [] and state["claimed_in"] == [{"statement_id": saved["id"], "payroll_month": NEXT}]
    # 1회만: 기본값에는 더 이상 나오지 않고, 다른 명세가 같은 항목을 요청하면 거부된다.
    assert _defaults()["settlement_items"] == {"pay_items": [], "deduction_items": []}
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(_next_payload(items), ADMIN)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        svc.cancel_payroll_payout_diff_resolution(under["id"], ADMIN)
    assert exc.value.status_code == 409


def test_next_month_recover_is_deduction_and_deleting_draft_restores_pending():
    over = _resolved("next_month_recover", 2800000)
    items = _defaults()["settlement_items"]
    assert [i["amount"] for i in items["deduction_items"]] == [100000]
    assert items["deduction_items"][0]["label"] == "전월 초과지급분 회수"
    saved = svc.save_payroll(_next_payload(items), ADMIN)
    assert saved["other_deduction"] == 100000 and saved["net_pay"] == 2900000
    assert "settlement_warnings" not in saved
    assert _left(over["id"]) == 0 and _state(over["id"])["settled_amount"] == 0 and _state(over["id"])["claimed_amount"] == 100000
    svc.delete_payroll(saved["id"], ADMIN)
    assert _left(over["id"]) == 100000 and _state(over["id"])["claimed_in"] == []
    assert _defaults()["settlement_items"]["deduction_items"]


def test_recover_over_half_of_net_warns_but_does_not_block():
    _resolved("next_month_recover", 2800000, gross=3000000)
    items = _defaults()["settlement_items"]  # 이번 달 급여가 작아 회수액(10만원)이 실수령의 1/2 를 넘는다.
    saved = svc.save_payroll(_next_payload(items, 150000), ADMIN)
    assert saved["status"] == "draft" and saved["net_pay"] == 50000
    assert saved["settlement_warnings"] and "절반" in saved["settlement_warnings"][0]


def test_recover_larger_than_this_months_pay_is_rejected_without_saving():
    _resolved("next_month_recover", 2800000, gross=3000000)
    before = len(svc._read_hr("payroll_statements", ADMIN))
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(_next_payload(gross=50000), ADMIN)
    assert exc.value.status_code == 400 and len(svc._read_hr("payroll_statements", ADMIN)) == before


def test_client_cannot_set_settlement_amount_and_server_rebuilds_it():
    under = _resolved()
    items = _defaults()["settlement_items"]
    items["pay_items"][0]["amount"] = 1
    saved = svc.save_payroll(_next_payload(items), ADMIN)
    assert [i["amount"] for i in _settlement_items(saved)] == [80880] and saved["gross_pay"] == 3080880
    assert _left(under["id"]) == 0


def test_settlement_is_added_even_if_client_omits_it_and_opt_out_carries_it_forward():
    under = _resolved()
    root = under["root_id"]
    nov = svc.save_payroll(_next_payload(), ADMIN)
    assert [i["amount"] for i in _settlement_items(nov)] == [80880] and nov["gross_pay"] == 3080880
    off = svc.save_payroll(_next_payload(id=nov["id"], settlement_opt_out=[root]), ADMIN)
    assert not _settlement_items(off) and off["gross_pay"] == 3000000 and _state(under["id"])["pending_amount"] == -80880
    # 요청에 opt-out 이 없으면(근태 초안 재생성 등) 기존 선택을 잇는다.
    assert not _settlement_items(svc.save_payroll(_next_payload(id=nov["id"]), ADMIN))
    # 이번 달에서 뺀 차액은 그다음 달 명세에 실린다.
    dec = svc.save_payroll(_next_payload(payroll_month="2026-12", pay_date="2026-12-10"), ADMIN)
    assert [i["amount"] for i in _settlement_items(dec)] == [80880]
    assert _left(under["id"]) == 0


def test_attendance_draft_path_is_covered_by_helper_and_defaults_only_for_locked_latest():
    _resolved()
    rows = svc._read_hr("payroll_statements", ADMIN)
    pay, ded = svc._settlement_draft_parts(rows, email=EMPLOYEE["email"], business_id="biz-1", month=NEXT)
    assert len(pay) == 1 and ded == []
    assert svc._settlement_draft_parts(rows, email=EMPLOYEE["email"], business_id="biz-1", month=MONTH) == ([], [])
    assert svc._settlement_pay_total({"pay_items": pay}) == 80880


def _simple(payload):
    return {key: value for key, value in payload.items() if key not in ("pay_items", "deduction_items")}


def test_simple_total_entry_with_pending_settlement_is_rejected_without_saving(monkeypatch):
    under = _resolved()
    writes = []
    real = svc._write_hr_record
    monkeypatch.setattr(svc, "_write_hr_record", lambda *args, **kw: writes.append(args) or real(*args, **kw))
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(_simple(_next_payload()), ADMIN)
    assert exc.value.status_code == 400 and "지급·공제 항목을 함께 보내야" in exc.value.detail
    assert writes == [] and _left(under["id"]) == -80880
    # 이번 달 제외를 고르면 서버가 줄을 붙이지 않으므로 합계만으로도 저장된다.
    assert svc.save_payroll({**_simple(_next_payload()), "settlement_opt_out": [under["root_id"]]}, ADMIN)["gross_pay"] == 3000000


def test_simple_other_deduction_with_recover_is_rejected():
    _resolved("next_month_recover", 2800000)
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(_simple(_next_payload(other_deduction=5000)), ADMIN)
    assert exc.value.status_code == 400 and "지급·공제 항목을 함께 보내야" in exc.value.detail


def test_totals_only_correction_of_paid_statement_with_fact_lines_is_rejected_without_saving(monkeypatch):
    under, items, done = _paid_next_with_settlement()
    writes = []
    monkeypatch.setattr(svc, "_write_hr_record", lambda *args, **kw: writes.append(args))
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll({**_simple(_next_payload(items)), "id": done["id"], "gross_pay": done["gross_pay"], "taxable_pay": done["gross_pay"], "memo": "정정"}, ADMIN)
    assert exc.value.status_code == 400 and "지급·공제 항목을 함께 보내야" in exc.value.detail
    assert writes == []


def test_totals_only_save_without_any_settlement_still_works():
    saved = svc.save_payroll(_simple(_next_payload()), ADMIN)
    assert saved["gross_pay"] == 3000000 and not _settlement_items(saved)


def test_opt_out_keeps_only_existing_chains():
    under = _resolved()
    saved = svc.save_payroll(_next_payload(settlement_opt_out=[under["root_id"], "no-such-root"]), ADMIN)
    assert saved["settlement_opt_out"] == [under["root_id"]]
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll(_next_payload(settlement_opt_out="all"), ADMIN)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("contract_type,wage_type", [("freelancer", "hourly"), ("regular", "hourly")])
def test_attendance_draft_gets_settlement_once_and_tax_ignores_it(monkeypatch, contract_type, wage_type):
    over = _resolved("next_month_recover", 2800000)
    employment = {"contract_id": "c1", "employment": {"contract_type": contract_type, "wage_type": wage_type, "wage": 13000}}
    monkeypatch.setattr(svc, "_derive_current_employment", lambda *a, **k: employment)
    monkeypatch.setattr(svc, "_require_business_for_tenant", lambda business, user: business)
    monkeypatch.setattr(svc, "_payroll_attendance_rows", lambda *a: [
        {"id": "a1", "work_date": "2026-11-02", "worked_minutes": 30000, "status": "approved"}])
    monkeypatch.setattr(svc, "_payroll_default_present_rows", lambda *a: [])
    request = {"payroll_month": NEXT, "employee_request_id": "join-1", "business_id": "biz-1"}
    first = svc.payroll_draft_from_attendance(request, ADMIN)
    second = svc.payroll_draft_from_attendance(request, ADMIN)  # 재생성해도 회수 항목과 기타 공제가 불어나지 않는다
    assert second["id"] == first["id"] and first["taxable_pay"] == 6500000 and second["taxable_pay"] == 6500000
    assert [(i["code"], i["amount"]) for i in _settlement_items(second)] == [("prior_month_overpaid_recover", 100000)]
    assert second["other_deduction"] == 100000 and second["net_pay"] == first["net_pay"]
    if contract_type == "freelancer":
        assert second["tax_withholding"] == 214500  # 3.3% 는 이번 달 용역비 기준
    assert _left(over["id"]) == 0


def test_legacy_statement_key_is_read_as_root():
    under = _resolved()
    saved = svc.save_payroll(_next_payload(settlement_opt_out=[under["root_id"]]), ADMIN)
    legacy = {**stored(saved["id"]), "gross_pay": 3080880, "net_pay": 3080880, "settlement_opt_out": [], "pay_items": [
        *saved["pay_items"], {"code": "other", "label": "전월 미지급분 지급", "amount": 80880, "calc_note": "", "settlement_of_statement_id": under["id"]},
    ]}
    svc._write_hr_record("payroll_statements", legacy, ADMIN)
    assert _left(under["id"]) == 0 and _state(under["id"])["claimed_amount"] == -80880
    resaved = svc.save_payroll({**legacy, "taxable_pay": 3000000}, ADMIN)
    assert [(i["code"], i["amount"]) for i in _settlement_items(resaved)] == [("prior_month_unpaid_pay", 80880)]
    assert resaved["gross_pay"] == 3080880 and "settlement_of_statement_id" not in str(resaved["pay_items"])


# --- 반려 재현 (a) 정정 초안이 정산을 가져가지 않는다 ---------------------------------


def _paid_next_with_settlement():
    under = _resolved()
    items = _defaults()["settlement_items"]
    saved = svc.save_payroll(_next_payload(items), ADMIN)
    done = svc.pay_payroll(svc.confirm_payroll(saved["id"], ADMIN)["id"], ADMIN)
    return under, items, done


def test_a_deleting_correction_draft_keeps_settlement_on_paid_statement():
    under, items, done = _paid_next_with_settlement()
    assert _state(under["id"])["pending_amount"] == 0
    assert svc.save_payroll(_next_payload(items, id=done["id"]), ADMIN)["id"] == done["id"]  # 내용이 같으면 새 버전 없음
    correction = svc.save_payroll(_next_payload(items, id=done["id"], memo="정정"), ADMIN)
    assert correction["id"] != done["id"] and correction["root_id"] == done["root_id"]
    assert [i["amount"] for i in _settlement_items(correction)] == [80880]
    state = _state(under["id"])  # 같은 체인의 두 버전은 한 번만 센다
    assert state["pending_amount"] == 0 and state["settled_amount"] == -80880 and len(state["settled_in"]) == 1
    svc.delete_payroll(correction["id"], ADMIN)
    state = _state(under["id"])
    assert state["pending_amount"] == 0 and state["settled_in"] == [{"statement_id": done["id"], "payroll_month": NEXT}]
    assert _defaults()["settlement_items"] == {"pay_items": [], "deduction_items": []}
    with pytest.raises(HTTPException) as exc:  # 다른 명세에 재정산 불가
        svc.save_payroll(_next_payload(items, payroll_month="2026-12", pay_date="2026-12-10"), ADMIN)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        svc.cancel_payroll_payout_diff_resolution(under["id"], ADMIN)
    assert exc.value.status_code == 409


def test_a_correction_draft_cannot_opt_out_of_amount_already_paid():
    under, items, done = _paid_next_with_settlement()
    correction = svc.save_payroll(_next_payload(items, id=done["id"], memo="정정", settlement_opt_out=[under["root_id"]]), ADMIN)
    assert [i["amount"] for i in _settlement_items(correction)] == [80880]
    assert "settlement_opt_out" not in correction


def test_a_correction_of_paid_statement_does_not_pull_in_new_pending_difference():
    done = svc.pay_payroll(svc.confirm_payroll(svc.save_payroll(_next_payload(), ADMIN)["id"], ADMIN)["id"], ADMIN)
    under = _resolved()  # 11월 명세가 지급된 뒤에 생긴 10월 차액
    assert under["payroll_month"] == MONTH
    assert svc.save_payroll(_next_payload(id=done["id"]), ADMIN)["id"] == done["id"]  # 내용이 같으면 새 버전을 만들지 않는다
    correction = svc.save_payroll(_next_payload(id=done["id"], memo="정정"), ADMIN)
    assert not _settlement_items(correction) and _state(under["id"])["pending_amount"] == -80880


# --- 반려 재현 (b) 정산 저장은 원 명세에 쓰지 않는다 ----------------------------------


def test_b_settlement_save_never_writes_origin_statement(monkeypatch):
    under = _resolved()
    before = stored(under["id"])
    items = _defaults()["settlement_items"]
    calls = []
    original = svc._write_hr_record

    def spy(name, record, user):
        calls.append(record["id"])
        return original(name, record, user)

    monkeypatch.setattr(svc, "_write_hr_record", spy)
    saved = svc.save_payroll(_next_payload(items), ADMIN)
    assert calls == [saved["id"]] and under["id"] not in calls
    svc.delete_payroll(saved["id"], ADMIN)
    assert calls == [saved["id"]]  # 삭제도 원 명세를 건드리지 않는다
    assert stored(under["id"]) == before
    resolution = stored(under["id"])["payout_diff_resolution"]
    assert "settled_statement_id" not in resolution and "settled_at" not in resolution


def test_b_failed_write_leaves_nothing_half_done(monkeypatch):
    under = _resolved()
    before = len(svc._read_hr("payroll_statements", ADMIN))

    def boom(name, rows):
        raise OSError("disk full")

    original = svc._write_file_rows
    monkeypatch.setattr(svc, "_write_file_rows", boom)
    with pytest.raises(OSError):
        svc.save_payroll(_next_payload(), ADMIN)
    monkeypatch.setattr(svc, "_write_file_rows", original)
    assert len(svc._read_hr("payroll_statements", ADMIN)) == before
    assert _state(under["id"])["pending_amount"] == -80880


# --- 반려 재현 (c) 기준월 명세 정정 승계 ----------------------------------------------


def _correct_base(row, gross):
    """기준월 명세의 금액을 고쳐 새 버전으로 확정한다. net = gross − 300,000."""
    return svc.confirm_payroll(svc.save_payroll({
        "id": row["id"], "employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-1", "branch": "본점",
        "payroll_month": MONTH, "pay_date": f"{MONTH}-10", "gross_pay": gross, "tax_withholding": 100000, "insurance_deduction": 200000,
        "other_deduction": 0, "pay_items": [{"code": "base", "label": "기본급", "amount": gross, "calc_note": "월 기본급"}],
        "deduction_items": [{"code": "income_tax", "label": "소득세", "amount": 100000}, {"code": "national_pension", "label": "국민연금", "amount": 200000}],
    }, ADMIN)["id"], ADMIN)


def test_c_correction_before_settlement_inherits_payout_and_settles_new_difference():
    under = _resolved()
    fixed = _correct_base(under, 3010000)
    assert fixed["id"] != under["id"] and fixed["root_id"] == under["root_id"] and fixed["net_pay"] == 2710000
    assert fixed["payout"] == under["payout"] and fixed["payout_diff_resolution"] == under["payout_diff_resolution"]
    assert _state(fixed["id"])["pending_amount"] == -90880
    assert [i["amount"] for i in _defaults()["settlement_items"]["pay_items"]] == [90880]  # 누락 없음
    saved = svc.save_payroll(_next_payload(), ADMIN)
    assert [i["amount"] for i in _settlement_items(saved)] == [90880] and _left(fixed["id"]) == 0


def test_c_correction_after_settlement_settles_only_the_remainder():
    under, _, done = _paid_next_with_settlement()
    fixed = _correct_base(under, 3010000)
    state = _state(fixed["id"])
    assert state["pending_amount"] == -10000 and state["settled_amount"] == -80880 and not state["resolution_invalid"]
    with pytest.raises(HTTPException) as exc:
        svc.cancel_payroll_payout_diff_resolution(fixed["id"], ADMIN)
    assert exc.value.status_code == 409
    dec = svc.save_payroll(_next_payload(payroll_month="2026-12", pay_date="2026-12-10"), ADMIN)
    assert [i["amount"] for i in _settlement_items(dec)] == [10000]  # 80,880 을 다시 지급하지 않는다
    assert _left(fixed["id"]) == 0 and len(_state(fixed["id"])["settled_in"]) == 1 and len(_state(fixed["id"])["claimed_in"]) == 1
    assert stored(done["id"])["pay_items"][-1]["amount"] == 80880


def test_c_correction_flipping_sign_invalidates_resolution_until_chosen_again():
    under, _, _ = _paid_next_with_settlement()
    fixed = _correct_base(under, 2970000)  # net 2,670,000 → 차액 −50,880, 이미 80,880 지급 → 30,000 초과
    state = _state(fixed["id"])
    assert state["pending_amount"] == 30000 and state["resolution_invalid"] is True
    assert svc.list_payroll(ADMIN, MONTH)[0]["resolution_invalid"] is True
    dec_payload = _next_payload(payroll_month="2026-12", pay_date="2026-12-10")
    assert not _settlement_items(svc.save_payroll(dec_payload, ADMIN))  # 무효 처리로는 항목을 만들지 않는다
    with pytest.raises(HTTPException) as exc:  # 남은 금액 방향과 맞는 방법만 고를 수 있다
        svc.set_payroll_payout_diff_resolution(fixed["id"], ADMIN, {"method": "next_month_pay"})
    assert exc.value.status_code == 400
    chosen = svc.set_payroll_payout_diff_resolution(fixed["id"], ADMIN, {"method": "next_month_recover"})
    assert chosen["payout_diff_resolution"]["diff_amount"] == 30000 and chosen["resolution_invalid"] is False
    dec = svc._read_hr("payroll_statements", ADMIN)
    dec = next(row for row in dec if row["payroll_month"] == "2026-12")
    resaved = svc.save_payroll(_next_payload(payroll_month="2026-12", pay_date="2026-12-10", id=dec["id"]), ADMIN)
    assert [(i["code"], i["amount"]) for i in _settlement_items(resaved)] == [("prior_month_overpaid_recover", 30000)]
    assert _left(fixed["id"]) == 0


def test_c_completed_direct_payment_survives_correction_and_resolution_change():
    under = _resolved("extra_pay")
    svc.complete_payroll_payout_diff_resolution(under["id"], ADMIN, "2026-11-11")
    assert _state(under["id"])["pending_amount"] == 0
    fixed = _correct_base(under, 3010000)
    assert fixed["payout_diff_completions"][0]["amount"] == -80880
    state = _state(fixed["id"])
    assert state["pending_amount"] == -10000 and state["resolution_invalid"] is True
    with pytest.raises(HTTPException) as exc:
        svc.complete_payroll_payout_diff_resolution(fixed["id"], ADMIN)
    assert exc.value.status_code == 409
    chosen = svc.set_payroll_payout_diff_resolution(fixed["id"], ADMIN, {"method": "extra_pay"})
    assert chosen["payout_diff_resolution"]["diff_amount"] == -10000 and "completed_at" not in chosen["payout_diff_resolution"]
    done = svc.complete_payroll_payout_diff_resolution(fixed["id"], ADMIN, "2026-11-20")
    assert [e["amount"] for e in done["payout_diff_completions"]] == [-80880, -10000] and done["pending_amount"] == 0


def test_c_correction_confirm_takes_latest_payout_from_previous_version():
    row = confirmed(raw_net=2619120)
    draft_v2 = svc.save_payroll({**{k: v for k, v in row.items() if k != "status"}, "memo": "정정"}, ADMIN)
    assert draft_v2["id"] != row["id"] and draft_v2["status"] == "draft"
    svc.set_payroll_payout_diff_resolution(row["id"], ADMIN, {"method": "extra_pay"})
    fixed = svc.confirm_payroll(draft_v2["id"], ADMIN)
    assert fixed["payout_diff_resolution"]["method"] == "extra_pay"
    assert stored(row["id"])["superseded_by"] == fixed["id"]


def test_c_paying_corrected_version_keeps_inherited_payout():
    row = confirmed()
    fixed = svc.pay_payroll(row["id"], ADMIN, paid_amount=2619120, paid_date="2026-10-10", paid_source="bank")
    version = _correct_base(fixed, 3000000 + 1000)
    assert version["status"] == "confirmed" and version["payout"]["amount"] == 2619120
    done = svc.pay_payroll(version["id"], ADMIN)
    assert done["payout"] == fixed["payout"] and done["pending_amount"] == 2619120 - 2701000


# --- 반려 재현 (d) 정산 항목의 code·source 고정 ---------------------------------------


def _four_insurance_items(month=NEXT):
    return svc.save_payroll(_next_payload(employment_tax_type="four_insurance", payroll_month=month, pay_date=f"{month}-10"), ADMIN)


def test_d_settlement_key_on_statutory_code_is_removed_and_reference_stays_single():
    plain = _four_insurance_items("2026-09")  # 같은 금액의 법정 공제 기준값(정산 없는 달)
    under = _resolved()
    pension = next(i for i in plain["deduction_items"] if i["code"] == "national_pension")
    forged = [{**i, "settlement_of_root_id": under["root_id"], "settlement_of_statement_id": under["id"]} if i["code"] == "national_pension" else i
              for i in plain["deduction_items"]]
    assert pension["source"] == "auto"
    saved = svc.save_payroll(_next_payload(
        employment_tax_type="four_insurance", deduction_items=forged, tax_withholding=plain["tax_withholding"],
        insurance_deduction=plain["insurance_deduction"],
    ), ADMIN)
    refs = _settlement_items(saved)
    assert [(i["code"], i["amount"], i["source"]) for i in refs] == [("prior_month_unpaid_pay", 80880, "settlement")]
    for field in ("tax_withholding", "insurance_deduction", "taxable_pay"):
        assert saved[field] == plain[field], field
    saved_pension = next(i for i in saved["deduction_items"] if i["code"] == "national_pension")
    assert saved_pension["amount"] == pension["amount"] and "settlement_of_root_id" not in saved_pension
    assert _left(under["id"]) == 0


def test_d_statutory_recalculation_preserves_recover_item_and_excludes_it_from_tax():
    plain = _four_insurance_items("2026-09")
    _resolved("next_month_recover", 2800000)
    saved = svc.save_payroll(_next_payload(employment_tax_type="four_insurance"), ADMIN)
    recover = [i for i in saved["deduction_items"] if i["code"] == "prior_month_overpaid_recover"]
    assert [(i["amount"], i["source"]) for i in recover] == [(100000, "settlement")]
    assert saved["other_deduction"] == 100000 and not [i for i in saved["deduction_items"] if i["code"] == "other"]
    for field in ("tax_withholding", "insurance_deduction", "taxable_pay"):
        assert saved[field] == plain[field], field
    resaved = svc.save_payroll({**saved, "id": saved["id"]}, ADMIN)  # 재계산을 다시 거쳐도 이중 계상 없음
    assert resaved["other_deduction"] == 100000 and len(_settlement_items(resaved)) == 1
    assert resaved["net_pay"] == saved["net_pay"]


def test_d_forged_settlement_shapes_are_dropped_or_stripped():
    under = _resolved()
    root = under["root_id"]
    extra = [
        {"code": "prior_month_unpaid_pay", "label": "위조", "amount": 999, "calc_note": "", "source": "settlement", "settlement_of_root_id": "other-root"},
        {"code": "other", "label": "위조2", "amount": 777, "calc_note": "", "settlement_of_root_id": root},
    ]
    payload = _next_payload()
    payload["pay_items"] = [*payload["pay_items"], *extra]
    payload["gross_pay"] += 999 + 777
    bonus = {"code": "bonus", "label": "상여", "amount": 5000, "calc_note": "", "source": "settlement", "settlement_of_root_id": root}
    payload["pay_items"].append(bonus)
    payload["gross_pay"] += 5000
    payload["taxable_pay"] += 5000
    saved = svc.save_payroll(payload, ADMIN)
    assert [(i["code"], i["amount"]) for i in _settlement_items(saved)] == [("prior_month_unpaid_pay", 80880)]
    kept = next(i for i in saved["pay_items"] if i["code"] == "bonus")
    assert kept["amount"] == 5000 and "settlement_of_root_id" not in kept and "source" not in kept
    assert saved["gross_pay"] == 3000000 + 5000 + 80880 and saved["taxable_pay"] == 3005000


def test_d_settlement_code_needs_server_built_reference():
    from app.services.yeoljeong_payroll_pdf import PayrollItemError, normalize_payroll_items

    with pytest.raises(PayrollItemError):
        normalize_payroll_items({"pay_items": [{"code": "prior_month_unpaid_pay", "label": "x", "amount": 1}]})
    cleaned = normalize_payroll_items({"pay_items": [
        {"code": "other", "label": "x", "amount": 1, "source": "settlement", "settlement_of_root_id": "r"},
    ]})["pay_items"][0]
    assert "source" not in cleaned and "settlement_of_root_id" not in cleaned


def test_list_and_versions_return_computed_values_and_hide_them_from_employee():
    under, _, done = _paid_next_with_settlement()
    row = next(item for item in svc.list_payroll(ADMIN, MONTH) if item["id"] == under["id"])
    assert row["pending_amount"] == 0 and row["settled_amount"] == -80880
    assert row["settled_in"] == [{"statement_id": done["id"], "payroll_month": NEXT}]
    assert svc.payroll_versions(under["id"], ADMIN)[0]["settled_in"] == row["settled_in"]
    assert svc.payroll_settlement_states(ADMIN)[under["id"]]["pending_amount"] == 0
    mine = svc.list_payroll(EMPLOYEE, MONTH)[0]
    for key in ("pending_amount", "settled_in", "claimed_amount", "claimed_in", "payout_diff_resolution", "payout_diff_completions", "resolution_invalid"):
        assert key not in mine


def test_computed_fields_from_client_are_not_stored():
    saved = draft(pending_amount=5, settled_amount=6, settled_in=[{"statement_id": "x"}], resolution_invalid=True, payout={"amount": 1})
    for key in ("pending_amount", "settled_amount", "settled_in", "resolution_invalid", "payout"):
        assert key not in stored(saved["id"])


def test_workspace_rows_get_computed_values_and_survive_failure(monkeypatch):
    import asyncio

    from app.api import obys_workspaces as workspaces

    under, _, _ = _paid_next_with_settlement()
    rows = asyncio.run(workspaces._with_payroll_settlement_state(ADMIN, [{"id": under["id"], "status": "paid"}, {"id": "legacy-1"}]))
    assert rows[0]["pending_amount"] == 0 and rows[0]["settled_amount"] == -80880 and rows[1] == {"id": "legacy-1"}

    def fail(user):
        raise RuntimeError("db down")

    monkeypatch.setattr(svc, "payroll_settlement_states", fail)
    assert asyncio.run(workspaces._with_payroll_settlement_state(ADMIN, [{"id": "a"}])) == [{"id": "a"}]


# --- 정산 불변식 I1~I7 --------------------------------------------------------------
# 부호는 I3 그대로다: diff = 비교 지급액 − net_pay. 양수는 더 지급(받을 돈), 음수는 덜 지급(줄 돈). 회수 항목은 settled + 쪽이다.

DEC = {"payroll_month": "2026-12", "pay_date": "2026-12-10"}
RECOVER, PAY = "prior_month_overpaid_recover", "prior_month_unpaid_pay"


def _paid_next_with_recover(**extra):
    """R 초과지급 100,000 을 11월 Q 에서 회수하고 Q 를 지급까지 끝낸다."""
    over = _resolved("next_month_recover", 2800000)
    saved = svc.save_payroll(_next_payload(**extra), ADMIN)
    return over, svc.pay_payroll(svc.confirm_payroll(saved["id"], ADMIN)["id"], ADMIN)


def _lines(row):
    return [(i["code"], i["amount"]) for i in _settlement_items(row)]


def _write_spy(monkeypatch):
    calls = []
    original = svc._write_hr_record

    def spy(name, record, user):
        calls.append(record["id"])
        return original(name, record, user)

    monkeypatch.setattr(svc, "_write_hr_record", spy)
    return calls


def test_t1_fact_survives_when_base_correction_flips_the_sign():
    over, done = _paid_next_with_recover()
    fixed = _correct_base(over, 3050000)  # net 2,750,000 → 초과지급 50,000 인데 이미 100,000 을 걷었다
    correction = svc.save_payroll(_next_payload(id=done["id"], memo="정정"), ADMIN)
    assert correction["id"] != done["id"] and correction["status"] == "draft"
    assert _lines(correction) == [(RECOVER, 100000)] and correction["other_deduction"] == 100000
    state = _state(fixed["id"])
    assert state["settled_amount"] == 100000 and state["pending_amount"] == -50000  # 회사가 50,000 을 더 걷었다 = 직원에게 줄 돈
    assert state["claimed_amount"] == 0 and state["resolution_invalid"] is True
    assert _defaults("2026-12")["settlement_items"] == {"pay_items": [], "deduction_items": []}
    assert not _settlement_items(svc.save_payroll(_next_payload(**DEC), ADMIN))
    assert stored(done["id"])["deduction_items"] == done["deduction_items"]


def test_t2_other_chains_fact_does_not_block_claim_for_the_remainder():
    over, done = _paid_next_with_recover()
    fixed = _correct_base(over, 2950000)  # net 2,650,000 → 차액 150,000, 사실 100,000
    assert _state(fixed["id"])["pending_amount"] == 50000 and not _state(fixed["id"])["resolution_invalid"]
    items = _defaults("2026-12")["settlement_items"]
    assert [i["amount"] for i in items["deduction_items"]] == [50000]
    saved = svc.save_payroll(_next_payload(items, **DEC), ADMIN)  # 클라이언트가 보내도 409 아님
    assert _lines(saved) == [(RECOVER, 50000)] and saved["id"] != done["id"]
    state = _state(fixed["id"])
    assert state["settled_amount"] == 100000 and state["claimed_amount"] == 50000
    assert state["settled_amount"] + state["claimed_amount"] == 150000


def test_t3_only_one_draft_chain_may_claim_a_root():
    under = _resolved()
    first = svc.save_payroll(_next_payload(), ADMIN)
    second = svc.save_payroll(_next_payload(**DEC), ADMIN)
    assert not _settlement_items(second) and second["gross_pay"] == 3000000
    forced = {"pay_items": _settlement_items(first), "deduction_items": []}
    before = stored(second["id"])
    for extra in ({"id": second["id"]}, {}):
        with pytest.raises(HTTPException) as exc:
            svc.save_payroll(_next_payload(forced, **DEC, **extra), ADMIN)
        assert exc.value.status_code == 409
    assert stored(second["id"]) == before and len(_state(under["id"])["claimed_in"]) == 1
    svc.delete_payroll(first["id"], ADMIN)  # 첫 청구가 사라지면 다음 저장에서 넘겨받는다
    assert _lines(svc.save_payroll(_next_payload(id=second["id"], **DEC), ADMIN)) == [(PAY, 80880)]


def _tamper(payload, field, how):
    line = next(i for i in payload[field] if i.get("settlement_of_root_id"))
    if how == "amount":
        line["amount"] = 1
    elif how == "root":
        line["settlement_of_root_id"] = "other-root"
    else:
        payload[field].remove(line)
    return payload


@pytest.mark.parametrize("how", ["amount", "root", "delete"])
def test_t4_client_cannot_change_fact_line_even_with_totals_left_as_is(how):
    under, items, done = _paid_next_with_settlement()
    payload = _tamper(_next_payload(items, id=done["id"], memo="정정"), "pay_items", how)
    correction = svc.save_payroll(payload, ADMIN)
    assert _settlement_items(correction) == _settlement_items(done) and correction["gross_pay"] == done["gross_pay"]
    assert correction["net_pay"] == done["net_pay"] and _state(under["id"])["settled_amount"] == -80880


@pytest.mark.parametrize("how", ["amount", "root", "delete"])
def test_t4_client_cannot_change_recover_fact_line(how):
    over, done = _paid_next_with_recover()
    items = {"pay_items": [], "deduction_items": [dict(i) for i in _settlement_items(done)]}
    payload = _tamper(_next_payload(items, id=done["id"], memo="정정"), "deduction_items", how)
    correction = svc.save_payroll(payload, ADMIN)
    assert _settlement_items(correction) == _settlement_items(done) and correction["other_deduction"] == 100000
    assert _state(over["id"])["settled_amount"] == 100000


def test_t4_client_total_lowered_with_the_line_keeps_fact_too():
    _, _, done = _paid_next_with_settlement()
    correction = svc.save_payroll(_next_payload(id=done["id"], memo="정정"), ADMIN)  # 줄과 합계를 같이 뺐다
    assert _settlement_items(correction) == _settlement_items(done) and correction["gross_pay"] == 3080880


def test_t5_statutory_recalculation_keeps_lines_and_tax_base():
    plain = _four_insurance_items("2026-09")
    over, done = _paid_next_with_recover(employment_tax_type="four_insurance")
    _correct_base(over, 2950000)
    correction = svc.save_payroll(_next_payload(id=done["id"], memo="정정", employment_tax_type="four_insurance"), ADMIN)
    assert _lines(correction) == [(RECOVER, 100000), (RECOVER, 50000)]
    again = {**correction}
    svc._apply_statutory_payroll_deductions(again, correction)
    assert _settlement_items(again) == _settlement_items(correction) and again["other_deduction"] == 150000
    for field in ("tax_withholding", "insurance_deduction", "taxable_pay"):
        assert again[field] == correction[field] == plain[field], field
    assert correction["net_pay"] == plain["net_pay"] - 150000


def test_t6_confirm_turns_recalculated_claim_into_fact_without_writing_origin(monkeypatch):
    over = _resolved("next_month_recover", 2800000)
    saved = svc.save_payroll(_next_payload(), ADMIN)
    assert _lines(saved) == [(RECOVER, 100000)]
    fixed = _correct_base(over, 2950000)  # 초안 저장 뒤 차액이 150,000 으로 늘었다
    before = stored(fixed["id"])
    calls = _write_spy(monkeypatch)
    done = svc.confirm_payroll(saved["id"], ADMIN)
    assert calls == [saved["id"]] and stored(fixed["id"]) == before
    assert _lines(done) == [(RECOVER, 150000)] and done["other_deduction"] == 150000 and done["net_pay"] == 2850000
    state = _state(fixed["id"])
    assert state["settled_amount"] == 150000 and state["pending_amount"] == 0 and state["claimed_amount"] == 0
    assert "payroll.settlement_recalculated" in [r["action"] for r in svc._read_file_rows(svc.EMPLOYMENT_AUDIT_LOG)]


def test_t6_confirm_drops_claim_that_no_longer_matches_direction():
    over = _resolved("next_month_recover", 2800000)
    saved = svc.save_payroll(_next_payload(), ADMIN)
    fixed = _correct_base(over, 3200000)  # 덜 지급으로 뒤집혔다
    done = svc.confirm_payroll(saved["id"], ADMIN)
    assert not _settlement_items(done) and done["other_deduction"] == 0 and done["net_pay"] == 3000000
    assert _state(fixed["id"])["pending_amount"] == -100000 and _state(fixed["id"])["resolution_invalid"] is True


def test_t6_confirm_rejects_recalculated_claim_larger_than_pay_and_leaves_draft():
    over = _resolved("next_month_recover", 2800000)
    saved = svc.save_payroll(_next_payload(gross=200000), ADMIN)
    _correct_base(over, 2700000)  # 회수액이 400,000 으로 늘어 이번 달 지급액을 넘는다
    with pytest.raises(HTTPException) as exc:
        svc.confirm_payroll(saved["id"], ADMIN)
    assert exc.value.status_code == 400 and stored(saved["id"])["status"] == "draft"
    assert _lines(stored(saved["id"])) == [(RECOVER, 100000)]


def test_i7_confirm_leaves_statement_without_settlement_untouched():
    saved = draft()
    done = svc.confirm_payroll(saved["id"], ADMIN)
    assert {k: v for k, v in done.items() if k not in ("status", "confirmed_by", "confirmed_at", "updated_at")} == {
        k: v for k, v in saved.items() if k not in ("status", "updated_at")}
    assert "payroll.settlement_recalculated" not in [r["action"] for r in svc._read_file_rows(svc.EMPLOYMENT_AUDIT_LOG)]


def test_i6_correction_keeps_fact_and_adds_separate_topup_line_then_both_become_facts():
    over, done = _paid_next_with_recover()
    fixed = _correct_base(over, 2950000)
    correction = svc.save_payroll(_next_payload(id=done["id"], memo="정정"), ADMIN)
    fact, topup = _settlement_items(correction)
    assert fact == _settlement_items(done)[0]
    assert (topup["label"], topup["amount"], topup["settlement_of_root_id"]) == ("전월 차액 추가 정산", 50000, over["root_id"])
    state = _state(fixed["id"])
    assert state["settled_amount"] == 100000 and state["pending_amount"] == 50000 and state["claimed_amount"] == 50000
    resaved = svc.save_payroll(_next_payload(id=correction["id"], memo="정정 2"), ADMIN)  # 다시 저장해도 사실은 그대로, 청구는 한 줄
    assert _settlement_items(resaved) == [fact, topup]
    final = svc.confirm_payroll(correction["id"], ADMIN)
    state = _state(fixed["id"])
    assert _lines(final) == [(RECOVER, 100000), (RECOVER, 50000)] and state["settled_amount"] == 150000 and state["pending_amount"] == 0
    nxt = svc.save_payroll(_next_payload(id=final["id"], memo="재정정"), ADMIN)
    assert _settlement_items(nxt) == _settlement_items(final) and _state(fixed["id"])["claimed_amount"] == 0


def test_i6_correction_topup_in_opposite_direction_after_rechoosing():
    over, done = _paid_next_with_recover()
    fixed = _correct_base(over, 3050000)
    chosen = svc.set_payroll_payout_diff_resolution(fixed["id"], ADMIN, {"method": "next_month_pay"})
    assert chosen["payout_diff_resolution"]["diff_amount"] == -50000 and chosen["resolution_invalid"] is False
    correction = svc.save_payroll(_next_payload(id=done["id"], memo="정정"), ADMIN)
    assert _lines(correction) == [(PAY, 50000), (RECOVER, 100000)]
    assert correction["gross_pay"] == 3050000 and correction["other_deduction"] == 100000
    state = _state(fixed["id"])
    assert state["settled_amount"] == 100000 and state["claimed_amount"] == -50000 and state["pending_amount"] == -50000


def test_i6_unchanged_resave_of_paid_statement_makes_no_draft_for_the_remainder():
    over, done = _paid_next_with_recover()
    _correct_base(over, 2950000)
    before = len(svc._read_hr("payroll_statements", ADMIN))
    assert svc.save_payroll(_next_payload(id=done["id"]), ADMIN)["id"] == done["id"]
    assert len(svc._read_hr("payroll_statements", ADMIN)) == before


def test_i6_topup_line_can_be_opted_out_but_fact_line_cannot():
    over, done = _paid_next_with_recover()
    _correct_base(over, 2950000)
    correction = svc.save_payroll(_next_payload(id=done["id"], memo="정정", settlement_opt_out=[over["root_id"]]), ADMIN)
    assert _settlement_items(correction) == _settlement_items(done) and correction["settlement_opt_out"] == [over["root_id"]]


def test_i4_own_claim_echoed_after_it_disappears_is_not_a_conflict():
    over, _ = _paid_next_with_recover()
    fixed = _correct_base(over, 2950000)
    saved = svc.save_payroll(_next_payload(**DEC), ADMIN)
    items = {"pay_items": [], "deduction_items": _settlement_items(saved)}
    _correct_base(fixed, 3000000)  # 차액이 100,000 으로 돌아와 잔액이 없어졌다
    resaved = svc.save_payroll(_next_payload(items, id=saved["id"], **DEC), ADMIN)
    assert not _settlement_items(resaved) and resaved["other_deduction"] == 0


def test_i4_resolution_cannot_be_cancelled_or_changed_while_a_draft_claims_it():
    under = _resolved()
    svc.save_payroll(_next_payload(), ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.set_payroll_payout_diff_resolution(under["id"], ADMIN, {"method": "extra_pay"})
    assert exc.value.status_code == 409


def test_i2_legacy_fact_line_is_copied_with_root_reference():
    under = _resolved()
    saved = svc.save_payroll(_next_payload(settlement_opt_out=[under["root_id"]]), ADMIN)
    done = svc.pay_payroll(svc.confirm_payroll(saved["id"], ADMIN)["id"], ADMIN)
    legacy_line = {"code": "other", "label": "전월 미지급분 지급", "amount": 80880, "calc_note": "", "settlement_of_statement_id": under["id"]}
    svc._write_hr_record("payroll_statements", {**stored(done["id"]), "gross_pay": 3080880, "net_pay": 3080880, "pay_items": [*done["pay_items"], legacy_line]}, ADMIN)
    assert _state(under["id"])["settled_amount"] == -80880
    correction = svc.save_payroll(_next_payload(id=done["id"], memo="정정"), ADMIN)
    assert _lines(correction) == [(PAY, 80880)] and _settlement_items(correction)[0]["settlement_of_root_id"] == under["root_id"]
    assert correction["gross_pay"] == 3080880 and _state(under["id"])["settled_amount"] == -80880
