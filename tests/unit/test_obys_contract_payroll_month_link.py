"""ACCT-OBYS-CONTRACT-PAYROLL-SYNC-20261105 — 계약일자=적용일, 급여월 기준 적용 계약 판정·급여 초안 연결.

파일 모드로만 돈다(운영 DB 미접촉). 서명 흐름을 거치지 않고 서명 완료 계약 행을 직접 심는다.
"""
import importlib.util
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMAIL = "member@example.com"


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "_run_db", _disable_db)
    clock = {"now": datetime(2026, 11, 5, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"].isoformat(timespec="seconds")

    monkeypatch.setattr(svc, "_now", fake_now)
    svc._write("employee_join_requests", [
        {"id": "join-a", "name": "직원", "email": EMAIL, "tenant_id": TENANT, "business_id": "biz-a",
         "branch": "A점", "status": "approved"},
        {"id": "join-b", "name": "직원", "email": EMAIL, "tenant_id": TENANT, "business_id": "biz-b",
         "branch": "B점", "status": "approved"},
    ])
    svc._write("contracts", [])
    return tmp_path


_SEQ = {"n": 0}


def contract(cid, contract_date, wage, *, business="biz-a", request="join-a", amends="", end_date="", meal=200000,
             start_date=None):
    _SEQ["n"] += 1
    return {
        "id": cid, "status": "signed", "contract_type": "regular", "wage_type": "monthly", "wage": wage,
        "non_tax_meal_allowance": meal, "contract_date": contract_date, "start_date": start_date or contract_date,
        "end_date": end_date, "employee_email": EMAIL, "employee_request_id": request, "business_id": business,
        "amends_contract_id": amends, "tenant_id": TENANT,
        "signed_at": f"2026-01-01T09:{_SEQ['n']:02d}:00+09:00", "meal_provision": "cash_no_meal",
        "employment_tax_type": "four_insurance",
    }


def put(*rows):
    svc._write("contracts", list(rows))


def derive(month, business="biz-a", request="join-a"):
    return svc._derive_current_employment(
        svc._read_hr("contracts", ADMIN), employee_email=EMAIL, employee_request_id=request,
        business_id=business, payroll_month=month,
    )


def chain():
    return [contract("c1", "2026-03-01", 3000000), contract("c2", "2026-10-15", 3100000, amends="c1")]


def test_month_uses_latest_contract_dated_on_or_before_month_end():
    put(*chain())
    assert derive("2026-05")["contract_id"] == "c1"
    assert derive("2026-09")["contract_id"] == "c1"
    # 변경계약 계약일자가 10-15 이므로 10월 급여는 변경계약, 이후 월도 변경계약
    assert derive("2026-10")["contract_id"] == "c2"
    assert derive("2027-02")["contract_id"] == "c2"


def test_superseded_original_has_effective_to_and_never_comes_back():
    put(*chain())
    first = derive("2026-05")
    assert first["effective_to"] == "2026-10-14"
    assert first["applies_from"] == "2026-03-01"
    # 원계약 종료일이 없어도 대체 이후 월에 원계약으로 되돌아가지 않는다
    assert derive("2026-12")["contract_id"] == "c2"
    assert derive("2026-12")["effective_to"] == ""


def test_month_before_first_contract_has_no_contract():
    put(*chain())
    assert derive("2026-02") is None


def test_businesses_are_judged_separately():
    put(*chain(), contract("b1", "2026-01-01", 2500000, business="biz-b", request="join-b"))
    assert derive("2026-11", business="biz-b", request="join-b")["contract_id"] == "b1"
    assert derive("2026-11", business="biz-a")["contract_id"] == "c2"
    assert derive("2026-02", business="biz-b", request="join-b")["contract_id"] == "b1"


def test_payroll_defaults_use_requested_month_not_today():
    put(*chain())
    may = svc.payroll_defaults(EMAIL, "2026-05", ADMIN, employee_request_id="join-a")
    assert may["source_contract_id"] == "c1"
    assert may["defaults"]["gross_pay"] == 3000000
    nov = svc.payroll_defaults(EMAIL, "2026-11", ADMIN, employee_request_id="join-a")
    assert nov["source_contract_id"] == "c2"
    assert nov["defaults"]["gross_pay"] == 3100000


def save(month, **extra):
    record, _ = svc._save_payroll_record(
        {"employee_email": EMAIL, "employee_request_id": "join-a", "business_id": "biz-a",
         "employee_name": "직원", "payroll_month": month, **extra}, ADMIN)
    return record


def test_draft_is_linked_to_month_contract_and_deviation_uses_same_contract():
    put(*chain())
    draft = save("2026-05", gross_pay=3050000, taxable_pay=2850000, non_tax_meal_allowance=200000)
    assert draft["source_contract_id"] == draft["contract_id"] == "c1"
    assert draft["contract_applies_to"] == "2026-10-14"
    # 이탈 판정 기준값도 같은 계약(c1: 3,000,000)이다
    assert draft["contract_deviation"]["gross_pay"]["contract"] == 3000000
    later = save("2026-11")
    assert later["source_contract_id"] == later["contract_id"] == "c2"
    assert later["gross_pay"] == 3100000


def test_contract_link_keeps_missing_meal_basis_as_none():
    put(contract("n1", "2026-03-01", 3000000, meal=""))
    derived = derive("2026-05")
    link = svc._payroll_contract_link(derived, "2026-05")
    assert link["non_tax_meal_allowance_basis"] is None
    assert svc._payroll_contract_link(None, "2026-05") == {}
    put(contract("n2", "2026-03-01", 3000000, meal=200000))
    assert svc._payroll_contract_link(derive("2026-05"), "2026-05")["non_tax_meal_allowance_basis"] == 200000


def test_client_cannot_forge_link_fields():
    put(*chain())
    draft = save("2026-05", contract_id="forged", contract_applies_from="1999-01-01")
    assert draft["contract_id"] == "c1"
    assert draft["contract_applies_from"] == "2026-03-01"


# --- 백필 --------------------------------------------------------------------


def load_backfill():
    path = Path(__file__).resolve().parents[2] / "scripts" / "acct_contract_supersede_backfill.py"
    spec = importlib.util.spec_from_file_location("acct_contract_supersede_backfill", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stale_rows(month_status):
    """연결 정보 없이(옛 방식으로) 저장된 급여 행."""
    base = {"employee_email": EMAIL, "employee_request_id": "join-a", "business_id": "biz-a", "tenant_id": TENANT,
            "employee_name": "직원", "gross_pay": 3000000, "tax_withholding": 0, "insurance_deduction": 0,
            "other_deduction": 0, "net_pay": 3000000, "version": 1}
    return [
        {**base, "id": "p-draft", "payroll_month": "2026-12", "status": "draft", "source_contract_id": "c1"},
        {**base, "id": "p-conf", "payroll_month": "2026-12", "status": month_status, "source_contract_id": "c1"},
    ]


def test_backfill_defaults_to_dry_run_and_writes_nothing(capsys):
    put(*chain())
    svc._write("payroll_statements", stale_rows("confirmed"))
    module = load_backfill()
    assert module.main(["--tenant", TENANT]) == 0
    assert "DRY-RUN" in capsys.readouterr().out
    assert all("contract_id" not in row for row in svc._read_file_rows("payroll_statements"))


def test_backfill_apply_links_draft_and_never_changes_locked_amounts():
    put(*chain())
    svc._write("payroll_statements", stale_rows("paid"))
    module = load_backfill()
    assert module.main(["--tenant", TENANT, "--apply"]) == 0
    rows = {row["id"]: row for row in svc._read_file_rows("payroll_statements")}
    assert rows["p-draft"]["contract_id"] == rows["p-draft"]["source_contract_id"] == "c2"
    paid = rows["p-conf"]
    assert paid["contract_id"] == "c2"
    assert paid["source_contract_id"] == "c1"  # 확정·지급분의 기존 근거는 바꾸지 않는다
    assert paid["gross_pay"] == 3000000 and paid["net_pay"] == 3000000 and paid["status"] == "paid"


def test_backfill_without_meal_basis_does_not_recompute_deviation():
    put(contract("n1", "2026-03-01", 3000000, meal=""))
    module = load_backfill()
    row = {"id": "p1", "status": "draft", "payroll_month": "2026-05", "gross_pay": 1, "contract_deviation": {"x": 1}}
    changes = module.compute_new_payload(row, derive("2026-05"))
    assert "contract_deviation" not in changes
    assert "non_tax_meal_allowance_basis" not in changes  # None 은 0 으로 채우지 않는다
    assert changes["contract_id"] == "n1"


def test_backfill_is_per_business():
    put(*chain(), contract("b1", "2026-01-01", 2500000, business="biz-b", request="join-b"))
    base = {"employee_email": EMAIL, "tenant_id": TENANT, "employee_name": "직원", "gross_pay": 1, "tax_withholding": 0,
            "insurance_deduction": 0, "other_deduction": 0, "net_pay": 1, "version": 1, "status": "draft",
            "payroll_month": "2026-12"}
    svc._write("payroll_statements", [
        {**base, "id": "pa", "business_id": "biz-a", "employee_request_id": "join-a"},
        {**base, "id": "pb", "business_id": "biz-b", "employee_request_id": "join-b"},
    ])
    assert load_backfill().main(["--tenant", TENANT, "--apply"]) == 0
    rows = {row["id"]: row for row in svc._read_file_rows("payroll_statements")}
    assert rows["pa"]["contract_id"] == "c2"
    assert rows["pb"]["contract_id"] == "b1"


# --- 재작업 1/2 반려 지적 ------------------------------------------------------


def test_two_amendments_of_same_original_end_original_at_earliest_one():
    """[P2] 같은 원계약을 10/15, 12/1 에 각각 변경하면 원계약은 10/14 에 끝난다(11/30 아님)."""
    rows = [contract("c1", "2026-03-01", 3000000),
            contract("c2", "2026-10-15", 3100000, amends="c1"),
            contract("c3", "2026-12-01", 3200000, amends="c1")]
    put(*rows)
    assert derive("2026-05")["contract_id"] == "c1"
    assert derive("2026-05")["effective_to"] == "2026-10-14"
    assert derive("2026-11")["contract_id"] == "c2"  # 11월에 원계약으로 되돌아가지 않는다
    assert derive("2026-12")["contract_id"] == "c3"


def freelancer(cid, contract_date, start, *, amends=""):
    return {**contract(cid, contract_date, 500000, amends=amends, start_date=start), "contract_type": "freelancer",
            "employment_tax_type": "freelancer_33", "wage_type": "case_fee", "freelancer_scope": "촬영",
            "freelancer_settlement_terms": "건별", "end_date": "2026-12-31"}


def test_freelancer_draft_uses_contract_for_requested_month_not_month_end_start_day(monkeypatch):
    """[P1] 시작일이 같은 원계약·변경계약(10월 계약일자) — 5월 정산은 원계약에 연결된다."""
    monkeypatch.setattr(svc, "_record_business_id", lambda record: str(record.get("business_id") or ""))
    put(freelancer("f1", "2026-03-01", "2026-03-01"), freelancer("f2", "2026-10-15", "2026-03-01", amends="f1"))
    draft = svc.payroll_draft_from_contract(
        {"employee_email": EMAIL, "employee_request_id": "join-a", "business_id": "biz-a",
         "payroll_month": "2026-05", "gross_pay": 500000, "amount_confirmed": True}, ADMIN)
    assert draft["source_contract_id"] == "f1"
    assert draft["contract_id"] == "f1"


def test_backfill_apply_does_not_overwrite_amount_edited_after_read(monkeypatch):
    """[P2] 조회 뒤 관리자가 draft 금액을 고쳐도 백필 저장이 오래된 금액으로 덮어쓰지 않는다."""
    put(*chain())
    svc._write("payroll_statements", stale_rows("paid")[:1])
    real_read = svc._read_hr
    state = {"first": True}

    def read_then_admin_edit(name, user):
        rows = real_read(name, user)
        if name == "payroll_statements" and state["first"]:
            state["first"] = False
            snapshot = [dict(r) for r in rows]
            edited = [{**r, "gross_pay": 4000000, "net_pay": 4000000} for r in real_read(name, user)]
            svc._write("payroll_statements", edited)  # 조회 직후 관리자 수정
            return snapshot
        return rows

    monkeypatch.setattr(svc, "_read_hr", read_then_admin_edit)
    assert load_backfill().main(["--tenant", TENANT, "--apply"]) == 0
    row = svc._read_file_rows("payroll_statements")[0]
    assert row["gross_pay"] == 4000000 and row["net_pay"] == 4000000
    assert row["contract_id"] == "c2"


# --- 재작업 2/2 반려 지적 ------------------------------------------------------


def test_later_sibling_amendment_supersedes_earlier_one_even_after_its_end_date():
    """[P1-2] c2(10/15)·c3(12/1)가 모두 c1 을 변경. c3 종료일 12/31 이후(다음 해 1월)에도 c2 로 되돌아가지 않는다."""
    c3 = contract("c3", "2026-12-01", 3200000, amends="c1", end_date="2026-12-31")
    put(contract("c1", "2026-03-01", 3000000), contract("c2", "2026-10-15", 3100000, amends="c1"), c3)
    assert derive("2026-11")["contract_id"] == "c2"
    assert derive("2026-11")["effective_to"] == "2026-11-30"
    assert derive("2026-12")["contract_id"] == "c3"
    assert derive("2027-01")["contract_id"] == "c3"
    assert derive("2027-01")["effective_to"] == ""


def _hold_lock_and_try(fn, *args):
    """직원 쓰기 잠금을 잡은 채 fn 을 다른 스레드에서 호출 — 잠금이 풀리기 전에는 끝나지 않아야 한다."""
    import threading
    import time
    done = threading.Event()
    result = {}

    def run():
        try:
            result["value"] = fn(*args)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc
        done.set()

    def holder():
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(0.5)
        result["finished_while_locked"] = done.is_set()
        return None

    svc._serialized_employee_write(EMAIL, holder)
    assert done.wait(10)
    return result


@pytest.mark.parametrize("action", ["confirm", "pay", "delete", "save"])
def test_payroll_writes_share_the_employee_lock_with_backfill(action):
    """[P1-1] 백필이 잡는 직원 잠금을 급여 저장·확정·지급·삭제 경로가 공유한다."""
    put(*chain())
    rows = stale_rows("confirmed")
    svc._write("payroll_statements", [rows[0], {**rows[1], "id": "p-conf2"}])
    calls = {
        "confirm": (svc.confirm_payroll, "p-draft", ADMIN),
        "pay": (svc.pay_payroll, "p-conf2", ADMIN),
        "delete": (svc.delete_payroll, "p-draft", ADMIN),
        "save": (svc.save_payroll, {"id": "p-draft", "employee_email": EMAIL, "employee_request_id": "join-a",
                                    "business_id": "biz-a", "payroll_month": "2026-12", "gross_pay": 3100000}, ADMIN),
    }
    result = _hold_lock_and_try(*calls[action])
    assert result["finished_while_locked"] is False, f"{action} 이 직원 잠금을 무시하고 실행됨"
    assert "error" not in result or not isinstance(result["error"], RuntimeError)


# --- CTO 직접 수정(재작업 2/2 반려 a32d4603 이후) ----------------------------------


def test_branched_amendments_never_revive_an_earlier_contract():
    """c2(10월)가 c1 을, c4(11월)가 c2 를, c3(12월)가 c1 을 변경. c3 종료(12/31) 뒤에도 c4 로 되돌아가지 않는다."""
    put(
        contract("c1", "2026-03-01", 3000000),
        contract("c2", "2026-10-01", 3100000, amends="c1"),
        contract("c4", "2026-11-01", 3150000, amends="c2"),
        contract("c3", "2026-12-01", 3200000, amends="c1", end_date="2026-12-31"),
    )
    assert derive("2026-10")["contract_id"] == "c2"
    assert derive("2026-11")["contract_id"] == "c4"
    assert derive("2026-11")["effective_to"] == "2026-11-30"
    assert derive("2026-12")["contract_id"] == "c3"
    assert derive("2027-01")["contract_id"] == "c3"


def test_same_contract_date_later_signed_wins():
    """같은 계약일자 변경계약이 둘이면 나중에 서명한 계약이 그 달 적용 계약이다."""
    put(
        contract("c1", "2026-03-01", 3000000),
        contract("c2", "2026-10-15", 3100000, amends="c1"),
        contract("c3", "2026-10-15", 3120000, amends="c1"),  # 더 늦게 서명(_SEQ)
    )
    assert derive("2026-10")["contract_id"] == "c3"
    assert derive("2026-12")["contract_id"] == "c3"
    assert derive("2026-09")["effective_to"] == "2026-10-14"


def _first_read_returns(monkeypatch, name, snapshot_rows):
    real_read = svc._read_hr
    state = {"first": True}

    def read(table, user):
        if table == name and state["first"]:
            state["first"] = False
            return [dict(r) for r in snapshot_rows]
        return real_read(table, user)

    monkeypatch.setattr(svc, "_read_hr", read)


def test_backfill_rejudges_with_payroll_month_edited_after_read(monkeypatch):
    """조회 뒤 관리자가 급여월을 12월→5월로 고치면, 5월 명세에는 5월 계약(c1)을 연결한다."""
    put(*chain())
    stale = stale_rows("paid")[:1]
    svc._write("payroll_statements", [{**stale[0], "payroll_month": "2026-05"}])
    _first_read_returns(monkeypatch, "payroll_statements", stale)  # dry-run 단계는 12월로 읽었다
    assert load_backfill().main(["--tenant", TENANT, "--apply"]) == 0
    row = svc._read_file_rows("payroll_statements")[0]
    assert row["payroll_month"] == "2026-05"
    assert row["contract_id"] == "c1"
    assert row["contract_payroll_month"] == "2026-05"


def test_backfill_uses_contract_signed_after_read(monkeypatch):
    """조회 뒤 12월 변경계약이 새로 서명되면, 12월 명세는 그 새 계약에 연결한다."""
    put(*chain(), contract("c5", "2026-12-01", 3300000, amends="c2"))
    svc._write("payroll_statements", stale_rows("paid")[:1])
    _first_read_returns(monkeypatch, "contracts", chain())  # 처음 읽을 때는 c5 가 없었다
    assert load_backfill().main(["--tenant", TENANT, "--apply"]) == 0
    row = svc._read_file_rows("payroll_statements")[0]
    assert row["contract_id"] == row["source_contract_id"] == "c5"
