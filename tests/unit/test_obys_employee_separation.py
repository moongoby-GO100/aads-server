"""퇴사·계약해지: 고용 원장, 계약 서명, 접근 권한 및 최종 정산 회귀 검사."""
import asyncio
import copy
import os
from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")
from app import auth
from app.api import obys_finance as api, obys_workspaces as workspace

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT,
         "current_membership": {"tenant_id": TENANT, "status": "active", "role": "owner"}}
EMPLOYEE = {"email": "member@example.com", "tenant_id": TENANT,
            "current_membership": {"tenant_id": TENANT, "status": "active", "role": "member"}}
PAYLOAD = {"separation_type": "resignation", "last_work_date": "2026-11-05", "reason": "개인 사정"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(svc, "_db_available", lambda: False)
    def no_db(coro):
        coro.close()
        return None
    monkeypatch.setattr(svc, "_run_db", no_db)
    @asynccontextmanager
    async def lock(*args):
        yield None
    monkeypatch.setattr(auth, "employee_membership_lock", lock)
    async def membership(record, action, user, **kwargs):
        return {"status": "removed" if action == "rejected" else "linked"}
    sync = AsyncMock(side_effect=membership)
    monkeypatch.setattr(svc, "sync_employee_tenant_membership", sync)
    svc._write("employee_join_requests", [{"id": "join", "tenant_id": TENANT, "business_id": "biz-mia",
        "branch": "열정국밥_미아점", "email": EMPLOYEE["email"], "name": "직원", "status": "approved",
        "membership_link": {"tenant_id": TENANT, "user_id": "account", "membership_id": "membership"}}])
    svc._write("contracts", [contract()])
    return sync


def contract(**overrides):
    return {"id": "contract", "tenant_id": TENANT, "employee_request_id": "join", "business_id": "biz-mia",
            "branch": "열정국밥_미아점", "employee_email": EMPLOYEE["email"], "status": "signed",
            "contract_type": "regular", "start_date": "2025-11-07", "end_date": "", "wage_type": "hourly", "wage": 12000,
            **overrides}


def employee():
    return svc._find(svc._read_hr("employee_join_requests", ADMIN), "join")


def separate(**overrides):
    return run(svc.separate_employee("join", {**PAYLOAD, **overrides}, ADMIN))


@pytest.mark.parametrize("kind", ["regular", "freelancer"])
def test_separation_and_final_settlement(kind):
    svc._write("contracts", [contract(contract_type=kind)])
    result = separate(separation_type="freelancer_termination" if kind == "freelancer" else "resignation")
    assert result["request"]["employment_status"] == "separated"
    assert result["membership"]["status"] == "removed"
    assert result["final_settlement"]["proration_review_required"] is True
    assert ("용역 대금 정산" in result["final_settlement"]["checklist"]) == (kind == "freelancer")
    saved = svc._read_hr("contracts", ADMIN)[0]
    assert saved["end_date"] == ""
    assert saved["effective_end_date"] == PAYLOAD["last_work_date"]


def test_revoke_pending_and_leave_registered_foreign_business():
    pending = contract(id="pending", status="requested", sign_token="secret", sign_token_hash="hash", requested_at="now")
    foreign = contract(id="foreign", employee_request_id="other", business_id="biz-eonni-naengmyeon", branch="성신여대역점")
    svc._write("contracts", [contract(), pending, foreign])
    original_digest = svc._signed_contract_snapshot(contract())[1]
    separate()
    rows = svc._read_hr("contracts", ADMIN)
    assert svc._signed_contract_snapshot(svc._find(rows, "contract"))[1] == original_digest
    saved = svc._find(rows, "pending")
    assert saved["status"] == "draft"
    assert not any(saved.get(key) for key in ("sign_token", "sign_token_hash", "requested_at"))
    assert svc._find(rows, "foreign") == foreign


def test_same_input_idempotent_change_date_only_and_cancel(isolated):
    first = separate()
    assert separate() == first
    assert isolated.await_count == 1
    with pytest.raises(HTTPException) as exc:
        separate(reason="변경")
    assert exc.value.status_code == 409
    assert separate(last_work_date="2026-11-06")["request"]["last_work_date"] == "2026-11-06"
    restored = run(svc.separate_employee("join", {}, ADMIN, cancel=True))
    assert restored["request"]["employment_status"] == "active"
    assert restored["membership"]["status"] == "linked"
    assert svc._read_hr("contracts", ADMIN)[0]["termination_history"][-1]["action"] == "cancel"
    assert run(svc.separate_employee("join", {}, ADMIN, cancel=True)) == restored


def test_other_active_employment_preserves_membership(isolated):
    other = {**employee(), "id": "other", "business_id": "biz-eonni-naengmyeon", "branch": "성신여대역점"}
    svc._write("employee_join_requests", [employee(), other])
    assert separate()["membership"]["status"] == "preserved"
    isolated.assert_not_awaited()
    assert svc._find(svc._read_hr("employee_join_requests", ADMIN), "other") == other


def test_membership_failure_compensates_hr_and_contracts(isolated):
    before, contracts = copy.deepcopy(employee()), svc._read_hr("contracts", ADMIN)
    isolated.side_effect = None
    isolated.return_value = {"status": "error"}
    with pytest.raises(HTTPException) as exc:
        separate()
    assert exc.value.status_code == 503
    assert employee() == before
    assert svc._read_hr("contracts", ADMIN) == contracts


@pytest.mark.parametrize("day,eligible", [("2026-11-05", False), ("2026-11-06", True)])
def test_severance_one_year_boundary(day, eligible):
    assert separate(last_work_date=day)["final_settlement"]["severance_eligible"] is eligible


def test_discontinuous_contracts_do_not_qualify():
    svc._write("contracts", [contract(id="old", start_date="2024-01-01", end_date="2025-11-01"),
                              contract(start_date="2026-01-01")])
    assert separate()["final_settlement"]["severance_eligible"] is False


def test_held_balance_warning(monkeypatch):
    seen = []
    def ledger(rows, candidate, *, owner):
        seen.append(owner)
        return [("2026-10", 330000), ("2026-11", -30000)]
    monkeypatch.setattr(svc, "_held_ledger", ledger)
    settlement = separate()["final_settlement"]
    assert settlement["held_balance"] == 300000
    assert settlement["warning"]
    assert seen == [{"employee_email": EMPLOYEE["email"], "business_id": "biz-mia"}]


def test_permissions_and_resignation_receipt():
    with pytest.raises(HTTPException) as exc:
        run(svc.separate_employee("join", PAYLOAD, EMPLOYEE))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        svc.submit_resignation(PAYLOAD, ADMIN)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        svc.list_resignations(EMPLOYEE)
    assert exc.value.status_code == 403
    assert svc.submit_resignation(PAYLOAD, EMPLOYEE)["status"] == "received"
    assert svc.list_resignations(ADMIN)[0]["employee_request_id"] == "join"
    separate()
    assert svc.list_resignations(ADMIN)[0]["status"] == "processed"
    with pytest.raises(HTTPException):
        svc.submit_resignation(PAYLOAD, EMPLOYEE)


@pytest.mark.parametrize("kind", ["regular", "freelancer"])
def test_separated_employees_payroll_attendance_and_signature_blocked(kind):
    svc._write("contracts", [contract(contract_type=kind)])
    assert svc.attendance_employee_allowed(EMPLOYEE, "biz-mia", date(2026, 11, 6))
    separate()
    assert svc.attendance_employee_allowed(EMPLOYEE, "biz-mia", date(2026, 11, 5))
    assert not svc.attendance_employee_allowed(EMPLOYEE, "biz-mia", date(2026, 11, 6))
    for draft in (svc.payroll_draft_from_contract, svc.payroll_draft_from_attendance):
        with pytest.raises(HTTPException) as exc:
            draft({"business_id": "biz-mia", "employee_request_id": "join", "payroll_month": "2026-12"}, ADMIN)
        assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        svc._require_employee_still_approved(contract(), EMPLOYEE)
    assert exc.value.status_code == 403


def test_active_freelancer_remains_attendance_target():
    svc._write("contracts", [contract(contract_type="freelancer")])
    assert svc.employee_work_date_allowed(employee(), date(2026, 12, 1))
    assert svc.attendance_employee_allowed(EMPLOYEE, "biz-mia", date(2026, 12, 1))


@pytest.mark.parametrize("day", [None, "2026-02-30", "2026-1-01", "invalid"])
def test_invalid_last_work_date(day):
    with pytest.raises(HTTPException) as exc:
        separate(last_work_date=day)
    assert exc.value.status_code == 422


def test_metadata_excluded_from_signed_snapshot():
    fields = {"terminated_at", "termination_reason", "effective_end_date", "termination_history",
              "separation_processed_at", "separation_original_end_date"}
    assert fields <= svc.CONTRACT_SNAPSHOT_EXCLUDED_FIELDS
    assert fields <= svc.CONTRACT_POST_SIGN_MUTABLE_FIELDS


def test_static_resignation_route_before_dynamic_employee_routes():
    paths = [route.path for route in api.router.routes]
    assert paths.index("/yeoljeong-finance/employees/resignations") < paths.index("/yeoljeong-finance/employees/{request_id}/separation")


def test_check_in_rejects_before_attendance_write(monkeypatch):
    monkeypatch.setattr(workspace, "_clock_business", AsyncMock(return_value={"id": "biz-mia"}))
    connect = AsyncMock()
    monkeypatch.setattr(workspace.upload_svc, "_connect", connect)
    separate(last_work_date="2020-01-01")
    with pytest.raises(HTTPException) as exc:
        run(workspace.attendance_check_in("biz-mia", workspace.AttendanceClockIn(), EMPLOYEE))
    assert exc.value.status_code == 403
    connect.assert_not_awaited()


def test_freelancer_settlement_period_trimmed(monkeypatch):
    svc._write("contracts", [contract(contract_type="freelancer", wage_type="case_fee", fee_basis="monthly")])
    separate(separation_type="freelancer_termination")
    captured = {}
    def save(payload, user, **kwargs):
        captured.update(kwargs)
        return {"id": "payroll"}, None
    monkeypatch.setattr(svc, "_save_payroll_record", save)
    monkeypatch.setattr(svc, "_payroll_with_totals", lambda row: row)
    svc.payroll_draft_from_contract({"employee_request_id": "join", "business_id": "biz-mia",
                                    "payroll_month": "2026-11", "gross_pay": 500000, "amount_confirmed": True}, ADMIN)
    assert captured["server_fields"]["settlement_basis"]["period_end"] == "2026-11-05"


@pytest.mark.parametrize("kind", ["regular", "freelancer"])
def test_post_separation_attendance_rows_excluded(monkeypatch, kind):
    svc._write("contracts", [contract(contract_type=kind, wage_type="hourly", fee_basis="hourly" if kind == "freelancer" else "")])
    separate()
    monkeypatch.setattr(svc, "_payroll_attendance_rows", lambda *args: [
        {"id": "before", "work_date": "2026-11-05", "status": "approved", "worked_minutes": 60},
        {"id": "after", "work_date": "2026-11-06", "status": "pending", "worked_minutes": 120}])
    monkeypatch.setattr(svc, "_payroll_default_present_rows", lambda *a: [])
    captured = {}
    def save(payload, user, **kwargs):
        captured.update(payload=payload, **kwargs)
        return {"id": "payroll"}, None
    monkeypatch.setattr(svc, "_save_payroll_record", save)
    monkeypatch.setattr(svc, "_audit_payroll", lambda *args: None)
    svc.payroll_draft_from_attendance({"employee_request_id": "join", "payroll_month": "2026-11"}, ADMIN)
    assert captured["payload"]["gross_pay"] == 12000
    assert captured["server_fields"]["attendance_basis"]["record_ids"] == ["before"]


@pytest.mark.parametrize("kind,separated", [("regular", True), ("freelancer", True), ("freelancer", False)])
def test_contract_schedules_cutoff_and_active_freelancer(monkeypatch, kind, separated):
    if separated:
        separate(last_work_date="2026-11-05")
    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield
        async def fetch(self, sql, *args):
            if "yeoljeong_contracts" in sql:
                return [{**contract(contract_type=kind), "contract_payload": {
                    "employee_request_id": "join", "daily_work_schedule": "목 09:00-18:00 (휴게 60분)\n금 09:00-18:00 (휴게 60분)"}}]
            return []
        async def close(self):
            pass
    monkeypatch.setattr(workspace, "_schedule_business", AsyncMock())
    monkeypatch.setattr(workspace.upload_svc, "_connect", AsyncMock(return_value=Connection()))
    result = run(workspace._contract_schedules("biz-mia", workspace.ContractSchedulesIn(
        date_from="2026-11-05", date_to="2026-11-06"), ADMIN))
    days = [row["work_date"] for row in result["employees"][0]["schedules"]]
    assert days == (["2026-11-05"] if separated else ["2026-11-05", "2026-11-06"])


def test_reinstatement_pending_membership_compensates(isolated):
    separate()
    before = copy.deepcopy(employee())
    isolated.side_effect = None
    isolated.return_value = {"status": "pending_account"}
    with pytest.raises(HTTPException) as exc:
        run(svc.separate_employee("join", {}, ADMIN, cancel=True))
    assert exc.value.status_code == 503
    assert employee() == before


def test_auth_transaction_commit_failure_compensates(monkeypatch):
    before = copy.deepcopy(employee())
    contracts = svc._read_hr("contracts", ADMIN)
    @asynccontextmanager
    async def failing_commit(*args):
        yield None
        raise RuntimeError("commit failed")
    monkeypatch.setattr(auth, "employee_membership_lock", failing_commit)
    with pytest.raises(HTTPException) as exc:
        separate()
    assert exc.value.status_code == 503
    assert employee() == before
    assert svc._read_hr("contracts", ADMIN) == contracts


def test_separation_gate_exact_methods_and_paths():
    from app.core.obys_tenant import _is_tenant_scoped_path
    base = "/api/v1/yeoljeong-finance"
    for method, suffix in [("POST", "/employees/request123/separation"),
                           ("POST", "/employees/request123/separation/cancel"),
                           ("POST", "/me/resignation"), ("GET", "/employees/resignations")]:
        assert _is_tenant_scoped_path(base + suffix, method)
        assert not _is_tenant_scoped_path(base + suffix + "/extra", method)
        assert not _is_tenant_scoped_path(base + suffix, "DELETE")


# ── 근무표 기본 출근(출근(기본))이 급여 초안·퇴사 규칙과 맞물리는지 ──
from datetime import time as _time


def _sched(day, start=(9, 0), end=(18, 0), brk=60):
    return {"work_date": date.fromisoformat(day), "start_time": _time(*start), "end_time": _time(*end),
            "break_minutes": brk, "kind": "work"}


def _use_default_schedules(monkeypatch, schedules, attendance):
    real = svc._payroll_default_present_rows

    def default_rows(*args):
        # 근무표 조회 구간에서만 DB 가 열려 있는 것처럼 둔다(직원 조회 등 다른 경로는 파일 원장 유지).
        with monkeypatch.context() as scope:
            scope.setattr(svc, "_db_available", lambda: True)
            scope.setattr(svc, "_db_fetch_payroll_schedules", lambda *a: None)
            scope.setattr(svc, "_run_db", lambda coro: schedules)
            return real(*args)
    monkeypatch.setattr(svc, "_payroll_default_present_rows", default_rows)
    monkeypatch.setattr(svc, "_payroll_attendance_rows", lambda *a: attendance)
    captured = {}

    def save(payload, user, **kwargs):
        captured.update(payload=payload, **kwargs)
        return {"id": "payroll"}, None
    monkeypatch.setattr(svc, "_save_payroll_record", save)
    monkeypatch.setattr(svc, "_audit_payroll", lambda *args: None)
    return captured


@pytest.mark.parametrize("kind", ["regular", "freelancer"])
def test_default_present_after_last_work_date_excluded(monkeypatch, kind):
    svc._write("contracts", [contract(contract_type=kind, wage_type="hourly", fee_basis="hourly" if kind == "freelancer" else "")])
    separate()  # 마지막 근무일 2026-11-05
    captured = _use_default_schedules(monkeypatch, [_sched("2026-11-04"), _sched("2026-11-05"), _sched("2026-11-06")], [])
    svc.payroll_draft_from_attendance({"employee_request_id": "join", "payroll_month": "2026-11"}, ADMIN)
    basis = captured["server_fields"]["attendance_basis"]
    assert basis["default_present_days"] == 2
    assert basis["default_present_dates"] == ["2026-11-04", "2026-11-05"]
    assert basis["approved_minutes"] == 960 and captured["payload"]["gross_pay"] == 192000
    assert basis["record_ids"] == []


def test_manual_absence_excludes_whole_day_and_real_rows_block_default(monkeypatch):
    captured = _use_default_schedules(
        monkeypatch, [_sched("2026-11-02"), _sched("2026-11-03"), _sched("2026-11-04")],
        [{"id": "abs", "work_date": "2026-11-03", "status": "rejected", "memo": "결근(대표 확인)", "worked_minutes": 0},
         {"id": "same-day", "work_date": "2026-11-03", "status": "approved", "worked_minutes": 60},
         {"id": "real", "work_date": "2026-11-04", "status": "approved", "worked_minutes": 120}])
    svc.payroll_draft_from_attendance({"employee_request_id": "join", "payroll_month": "2026-11"}, ADMIN)
    basis = captured["server_fields"]["attendance_basis"]
    assert basis["default_present_dates"] == ["2026-11-02"]  # 11-03 결근 지정, 11-04 는 실제 기록
    assert basis["approved_minutes"] == 480 + 120 and basis["record_ids"] == ["real"]
    assert basis["approved_days"] == 2


def test_month_of_only_absences_still_refuses_zero_draft(monkeypatch):
    _use_default_schedules(monkeypatch, [_sched("2026-11-02")],
                           [{"id": "abs", "work_date": "2026-11-02", "status": "rejected", "memo": "결근(대표 확인)"}])
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_attendance({"employee_request_id": "join", "payroll_month": "2026-11"}, ADMIN)
    assert exc.value.status_code == 409 and "확정된 근태가 없어" in exc.value.detail


def test_payroll_default_dates_match_schedule_judgment(monkeypatch):
    separate()
    schedules = [_sched("2026-11-02"), _sched("2026-11-03"), _sched("2026-11-04"), _sched("2026-11-06")]
    captured = _use_default_schedules(
        monkeypatch, schedules,
        [{"id": "abs", "work_date": "2026-11-03", "status": "rejected", "memo": "결근(대표 확인)"}])
    svc.payroll_draft_from_attendance({"employee_request_id": "join", "payroll_month": "2026-11"}, ADMIN)
    basis = captured["server_fields"]["attendance_basis"]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Seoul"))
    shown = [s["work_date"].isoformat() for s in schedules if workspace.attendance_schedule_judgment(
        {**s, "actual_records": [{"status": "rejected", "memo": "결근(대표 확인)", "work_date": "2026-11-03"}]
         if s["work_date"].isoformat() == "2026-11-03" else []},
        now=now, last_work_date="2026-11-05")["labels"] == ["출근(기본)"]]
    assert shown == basis["default_present_dates"] == ["2026-11-02", "2026-11-04"]
