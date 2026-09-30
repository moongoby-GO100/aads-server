"""AADS-OBYS-CONTRACT-TO-EMPLOYMENT-PAYROLL-20260930 — 서명 계약 → 고용조건 스냅샷 → 급여 기본값."""
import asyncio
import base64
import os
from datetime import datetime, timedelta
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, yeoljeong_ops_service

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
THIRD_PARTY = {"email": "other@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
OTHER_TENANT_ADMIN = {
    "email": "boss@other.example.com",
    "is_admin": True,
    "tenant_id": OTHER_TENANT,
    "current_membership": {"tenant_id": OTHER_TENANT, "status": "active"},
}


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """파일 모드로만 돈다 — 운영 DB·업로드 경로·실제 알림 발송을 건드리지 않는다."""
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS",
                 "OBYS_CONTRACT_PDF_FONT_PATH", "OBYS_CONTRACT_SIGN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)

    # 서명 시각이 같은 초에 겹치지 않도록 1분씩 흐르는 시계.
    clock = {"now": datetime(2026, 9, 30, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"].isoformat(timespec="seconds")

    monkeypatch.setattr(svc, "_now", fake_now)

    async def fake_create_notification(**kwargs):
        return {"id": "ntf-test", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)

    svc._write("employee_join_requests", [{
        "id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"],
        "address": "서울시 직원 주소", "phone": "010-1234-5678", "birth_date": "1990-01-01",
        "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved",
    }])
    return tmp_path


BASE = {
    "employee_request_id": "join-mia", "business_id": "biz-mia", "branch": "열정국밥_미아점",
    "contract_date": "2026-09-30", "start_date": "2026-10-01", "workplace": "열정국밥 미아점",
}
EMPLOYMENT_TERMS = {
    "employment_tax_type": "four_insurance", "job_description": "매장 운영",
    "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
    "work_days": "월~금 주 5일", "holidays": "매주 일요일", "pay_date": "매월 10일",
    "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
    "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
    "insurance_terms": "4대보험 법정 기준 적용",
}


def monthly_contract(**overrides):
    payload = {
        **BASE, **EMPLOYMENT_TERMS, "contract_type": "regular", "wage_type": "monthly", "wage": 2800000,
        "base_salary": 2600000, "non_tax_meal_allowance": 200000, "taxable_allowance": 0,
        "meal_provision": "cash_no_meal",
    }
    payload.update(overrides)
    return payload


def hourly_contract(**overrides):
    payload = {
        **BASE, **EMPLOYMENT_TERMS, "contract_type": "part_time", "wage_type": "hourly", "wage": 11000,
        "weekly_hours": "주 20시간", "work_days": "월·수·금", "daily_work_schedule": "월수금 각 6시간 40분",
        "meal_provision": "employer_meal",
    }
    payload.update(overrides)
    return payload


def freelancer_contract(**overrides):
    payload = {
        **BASE, "contract_type": "freelancer", "employment_tax_type": "freelancer_33", "wage_type": "case_fee",
        "wage": 500000, "freelancer_scope": "메뉴 사진 촬영", "freelancer_settlement_terms": "건별 정산",
    }
    payload.update(overrides)
    return payload


def confidentiality_contract(**overrides):
    payload = {**BASE, "contract_type": "confidentiality", "employment_tax_type": "four_insurance"}
    payload.update(overrides)
    return payload


def _signature_png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def sign(payload):
    saved = svc.save_contract(payload, ADMIN)
    requested = svc.request_contract_signature(saved["id"], ADMIN)
    return svc.sign_contract_and_deliver(
        {
            "token": requested["sign_token"],
            "signer_name": "가입 직원",
            "consent": True,
            "consent_version": "yeoljeong-contract-sign-v1",
            "signature_data_uri": "data:image/png;base64," + base64.b64encode(_signature_png()).decode("ascii"),
        },
        EMPLOYEE,
    )


def employee():
    return next(row for row in svc._read_hr("employee_join_requests", ADMIN) if row["id"] == "join-mia")


def audit_rows():
    return svc._read_file_rows(svc.EMPLOYMENT_AUDIT_LOG)


def _client(user):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


# --- A. 고용조건 스냅샷 ------------------------------------------------------


def test_signed_employment_contract_is_reflected_in_employee_snapshot():
    result = sign(monthly_contract())
    contract = result["contract"]

    assert contract["status"] == "signed"
    assert result["employment"]["status"] == "updated"
    assert result["employment"]["action"] == "employment.snapshot_created"
    row = employee()
    snapshot = row["current_employment"]
    assert row["current_employment_contract_id"] == contract["id"]
    assert snapshot["wage"] == 2800000
    assert snapshot["wage_type"] == "monthly"
    assert snapshot["start_date"] == "2026-10-01"
    assert snapshot["work_days"] == "월~금 주 5일"
    assert snapshot["weekly_hours"] == "주 40시간"
    assert snapshot["non_tax_meal_allowance"] == 200000
    assert snapshot["meal_provision"] == "cash_no_meal"
    assert snapshot["business_id"] == "biz-mia"
    assert row["current_employment_effective_from"] == "2026-10-01"
    assert row["current_employment_synced_at"]
    assert row["current_employment_error"] == ""

    listed = svc.list_approved_employees(ADMIN)[0]
    assert listed["current_employment"] == snapshot
    assert listed["current_employment_contract_id"] == contract["id"]
    assert listed["needs_employment_sync"] is False
    # 화면이 쓰는 기존 필드는 그대로 남는다.
    assert listed["contract_count"] == 1
    assert listed["needs_contract"] is False

    (entry,) = audit_rows()
    assert entry["action"] == "employment.snapshot_created"
    assert entry["resource_type"] == "employee_employment"
    assert entry["resource_id"] == "join-mia"
    assert entry["details"]["contract_id"] == contract["id"]
    assert entry["details"]["tenant_id"] == TENANT


def test_confidentiality_signature_does_not_create_employment_snapshot():
    result = sign(confidentiality_contract())

    assert result["contract"]["status"] == "signed"
    assert result["employment"]["status"] == "skipped"
    row = employee()
    assert not row.get("current_employment")
    assert not row.get("current_employment_contract_id")
    assert audit_rows() == []
    assert svc.list_approved_employees(ADMIN)[0]["needs_employment_sync"] is False


def test_confidentiality_after_employment_keeps_employment_snapshot():
    first = sign(monthly_contract())["contract"]
    sign(confidentiality_contract())
    assert employee()["current_employment_contract_id"] == first["id"]
    assert len(audit_rows()) == 1


def test_correction_contract_replaces_snapshot_and_keeps_history():
    first = sign(monthly_contract())["contract"]
    second_result = sign(monthly_contract(wage=3000000, base_salary=2800000, work_days="화~토 주 5일"))
    second = second_result["contract"]

    assert second_result["employment"]["action"] == "employment.snapshot_replaced"
    row = employee()
    assert row["current_employment_contract_id"] == second["id"]
    assert row["current_employment"]["wage"] == 3000000
    assert row["current_employment"]["work_days"] == "화~토 주 5일"
    assert row["previous_employment_contract_id"] == first["id"]

    # 이전 서명 계약서는 그대로 남고 불변이다.
    contracts = {item["id"]: item for item in svc.list_contracts(ADMIN)}
    assert contracts[first["id"]]["status"] == "signed"
    assert contracts[first["id"]]["wage"] == 2800000
    with pytest.raises(svc.HTTPException) as edit:
        svc.save_contract({**contracts[first["id"]], "wage": 1}, ADMIN)
    assert edit.value.status_code == 409

    actions = [entry["action"] for entry in audit_rows()]
    assert sorted(actions) == ["employment.snapshot_created", "employment.snapshot_replaced"]
    replaced = next(entry for entry in audit_rows() if entry["action"] == "employment.snapshot_replaced")
    assert replaced["details"]["previous_contract_id"] == first["id"]
    assert replaced["details"]["changes"]["wage"] == {"before": 2800000, "after": 3000000}

    history = svc.employee_employment_history("join-mia", ADMIN)
    assert [item["contract_id"] for item in history["contracts"]] == [first["id"], second["id"]]
    kinds = [item["kind"] for item in history["timeline"]]
    assert kinds.count("contract_signed") == 2 and kinds.count("audit") == 2
    stamps = [svc._pg_ts(item["at"]) for item in history["timeline"]]
    assert stamps == sorted(stamps)


def test_snapshot_failure_keeps_signature_and_records_error(monkeypatch):
    original = svc._derive_current_employment

    def broken(*args, **kwargs):
        raise RuntimeError("snapshot store down")

    monkeypatch.setattr(svc, "_derive_current_employment", broken)
    result = sign(monthly_contract())

    assert result["contract"]["status"] == "signed"
    assert result["employment"]["status"] == "failed"
    assert result["employment"]["error_recorded"] is True
    stored = next(item for item in svc.list_contracts(ADMIN) if item["id"] == result["contract"]["id"])
    assert stored["status"] == "signed"
    row = employee()
    assert "snapshot store down" in row["current_employment_error"]
    assert not row.get("current_employment")

    monkeypatch.setattr(svc, "_derive_current_employment", original)
    assert svc.list_approved_employees(ADMIN)[0]["needs_employment_sync"] is True

    resynced = _client(ADMIN).post("/yeoljeong-finance/employees/approved/join-mia/resync-employment")
    assert resynced.status_code == 200
    body = resynced.json()
    assert body["result"]["action"] == "employment.snapshot_resynced"
    assert body["current_employment_contract_id"] == result["contract"]["id"]
    assert body["current_employment_error"] == ""
    assert svc.list_approved_employees(ADMIN)[0]["needs_employment_sync"] is False
    # 두 번째 재동기화는 바뀐 게 없으므로 쓰지 않는다.
    again = svc.resync_employee_employment("join-mia", ADMIN)
    assert again["result"]["status"] == "unchanged"
    assert [entry["action"] for entry in audit_rows()] == ["employment.snapshot_resynced"]


def test_audit_db_path_reuses_ops_insert(monkeypatch):
    calls = []

    class FakeConn:
        async def close(self):
            calls.append("closed")

    async def fake_connect(*args, **kwargs):
        return FakeConn()

    async def fake_insert(conn, **kwargs):
        calls.append(kwargs)
        return {"id": "aud-1", **kwargs}

    import asyncpg

    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_db_url", lambda: "postgres://fake")
    monkeypatch.setattr(svc, "_run_db", asyncio.run)
    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    monkeypatch.setattr(yeoljeong_ops_service, "_insert_audit_log_conn", fake_insert)

    assert svc._append_employment_audit(
        tenant_id=TENANT, business_id="biz-mia", actor="owner@example.com",
        action="employment.snapshot_resynced", resource_type="employee_employment",
        resource_id="join-mia", details={"contract_id": "c-1"},
    ) is True
    assert calls[0]["action"] == "employment.snapshot_resynced"
    assert calls[0]["details"] == {"contract_id": "c-1", "tenant_id": TENANT}
    assert calls[-1] == "closed"


# --- B. 급여 기본값 ---------------------------------------------------------


def _defaults(user=ADMIN, month="2026-10"):
    return _client(user).get(
        "/yeoljeong-finance/payroll/defaults",
        params={"employee_email": EMPLOYEE["email"], "payroll_month": month},
    )


def test_monthly_payroll_defaults_fill_contract_wage_and_meal_split():
    contract = sign(monthly_contract())["contract"]
    response = _defaults()
    assert response.status_code == 200
    body = response.json()
    assert body["source_contract_id"] == contract["id"]
    assert body["wage_type"] == "monthly"
    assert body["defaults"] == {
        "employment_tax_type": "four_insurance",
        "meal_provision": "cash_no_meal",
        "gross_pay": 2800000,
        "taxable_pay": 2600000,
        "non_tax_meal_allowance": 200000,
    }
    assert "2,800,000" in body["basis"]
    assert body["employment_snapshot_in_sync"] is True


def test_monthly_defaults_distinguish_employer_provided_meal():
    sign(monthly_contract(base_salary=2800000, non_tax_meal_allowance=0, meal_provision="employer_meal"))
    defaults = _defaults().json()["defaults"]
    assert defaults["meal_provision"] == "employer_meal"
    assert defaults["non_tax_meal_allowance"] == 0
    assert defaults["taxable_pay"] == 2800000


def test_hourly_payroll_defaults_do_not_estimate_gross_pay():
    sign(hourly_contract())
    body = _defaults().json()
    assert "gross_pay" not in body["defaults"]
    assert "taxable_pay" not in body["defaults"]
    assert body["estimated"] is False
    assert body["reason"] == "시급제는 근태 확정 후 산정"
    assert body["hourly_wage"] == 11000
    assert body["weekly_hours"] == "주 20시간"

    saved = svc.save_payroll(
        {"employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-mia", "payroll_month": "2026-10"},
        ADMIN,
    )
    assert saved["gross_pay"] == 0
    assert "gross_pay" not in saved["contract_defaults_applied"]


def test_freelancer_payroll_defaults_only_tax_type():
    sign(freelancer_contract())
    body = _defaults().json()
    assert body["defaults"] == {"employment_tax_type": "freelancer_33"}
    assert body["wage_type"] == "case_fee"

    saved = svc.save_payroll(
        {"employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-mia", "payroll_month": "2026-10"},
        ADMIN,
    )
    assert saved["employment_tax_type"] == "freelancer_33"
    assert saved["contract_defaults_applied"] == ["employment_tax_type"]
    assert saved["gross_pay"] == 0


def test_save_payroll_fills_only_missing_values_from_contract():
    contract = sign(monthly_contract())["contract"]
    saved = svc.save_payroll(
        {"employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-mia",
         "payroll_month": "2026-10", "tax_withholding": 50000},
        ADMIN,
    )
    assert saved["gross_pay"] == 2800000
    assert saved["taxable_pay"] == 2600000
    assert saved["non_tax_meal_allowance"] == 200000
    assert saved["net_pay"] == 2750000
    assert "status" not in saved  # 자동 확정하지 않는다 — 상태는 관리자가 정한다
    assert saved["source_contract_id"] == contract["id"]
    assert set(saved["contract_defaults_applied"]) == {
        "gross_pay", "taxable_pay", "non_tax_meal_allowance", "meal_provision", "employment_tax_type",
    }
    assert saved["contract_deviation"] == {}
    actions = [entry["action"] for entry in audit_rows() if entry["resource_type"] == "payroll_statement"]
    assert actions == ["payroll.contract_defaults_applied"]


def test_save_payroll_never_overwrites_admin_values_and_records_deviation():
    sign(monthly_contract())
    saved = svc.save_payroll(
        {"employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-mia",
         "payroll_month": "2026-10", "gross_pay": 2500000, "taxable_pay": 2500000, "non_tax_meal_allowance": 0},
        ADMIN,
    )
    assert saved["gross_pay"] == 2500000
    assert saved["taxable_pay"] == 2500000
    assert saved["non_tax_meal_allowance"] == 0
    assert "gross_pay" not in saved["contract_defaults_applied"]
    assert saved["contract_deviation"]["gross_pay"] == {"contract": 2800000, "input": 2500000}
    assert saved["contract_deviation"]["non_tax_meal_allowance"] == {"contract": 200000, "input": 0}
    deviation_logs = [entry for entry in audit_rows() if entry["action"] == "payroll.contract_deviation"]
    assert len(deviation_logs) == 1
    assert deviation_logs[0]["resource_id"] == saved["id"]
    assert deviation_logs[0]["details"]["deviation"]["gross_pay"]["input"] == 2500000


def test_admin_gross_without_split_is_not_forced_into_contract_split():
    sign(monthly_contract())
    saved = svc.save_payroll(
        {"employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-mia", "gross_pay": 3100000},
        ADMIN,
    )
    assert saved["gross_pay"] == 3100000
    assert saved["taxable_pay"] == 0 and saved["non_tax_meal_allowance"] == 0
    assert saved["contract_deviation"] == {"gross_pay": {"contract": 2800000, "input": 3100000}}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"gross_pay": 2800000, "taxable_pay": 2500000, "non_tax_meal_allowance": 200000}, "합계"),
        ({"gross_pay": 2800000, "taxable_pay": 2550000, "non_tax_meal_allowance": 250000}, "200,000"),
        ({"gross_pay": 2800000, "taxable_pay": 2600000, "non_tax_meal_allowance": 200000, "meal_provision": "employer_meal"}, "식사를 제공"),
    ],
)
def test_existing_payroll_validations_still_apply_with_contract_defaults(overrides, message):
    sign(monthly_contract())
    with pytest.raises(svc.HTTPException) as exc:
        svc.save_payroll(
            {"employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-mia", **overrides},
            ADMIN,
        )
    assert exc.value.status_code == 400
    assert message in exc.value.detail


def test_employer_meal_contract_default_blocks_cash_non_tax_meal():
    sign(monthly_contract(base_salary=2800000, non_tax_meal_allowance=0, meal_provision="employer_meal"))
    with pytest.raises(svc.HTTPException) as exc:
        svc.save_payroll(
            {"employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-mia",
             "gross_pay": 2800000, "taxable_pay": 2600000, "non_tax_meal_allowance": 200000},
            ADMIN,
        )
    assert exc.value.status_code == 400
    assert "식사를 제공" in exc.value.detail


# --- 권한 ------------------------------------------------------------------


def test_non_admin_and_other_tenant_are_forbidden():
    sign(monthly_contract())
    resync = "/yeoljeong-finance/employees/approved/join-mia/resync-employment"
    history = "/yeoljeong-finance/employees/approved/join-mia/employment-history"

    for user in (EMPLOYEE, THIRD_PARTY, OTHER_TENANT_ADMIN):
        assert _defaults(user).status_code == 403
        assert _client(user).post(resync).status_code == 403
    for user in (THIRD_PARTY, OTHER_TENANT_ADMIN):
        assert _client(user).get(history).status_code == 403

    # 라우터 게이트와 별개로 서비스 자체도 다른 테넌트를 막는다.
    for call in (
        lambda: svc.payroll_defaults(EMPLOYEE["email"], "2026-10", OTHER_TENANT_ADMIN),
        lambda: svc.resync_employee_employment("join-mia", OTHER_TENANT_ADMIN),
        lambda: svc.employee_employment_history("join-mia", OTHER_TENANT_ADMIN),
        lambda: svc.employee_employment_history("join-mia", THIRD_PARTY),
    ):
        with pytest.raises(svc.HTTPException) as exc:
            call()
        assert exc.value.status_code == 403

    own = _client(EMPLOYEE).get(history)
    assert own.status_code == 200
    assert own.json()["current_employment"]["wage"] == 2800000
    assert _client(ADMIN).get(history).status_code == 200


def test_payroll_month_format_is_validated():
    sign(monthly_contract())
    assert _defaults(month="2026-13").status_code == 400
    assert _defaults(month="2026-09").json()["period_note"] == "급여 월이 계약 기간 밖입니다"


# --- D. 마이그레이션·백필 ----------------------------------------------------


def test_migration_is_schema_only_and_held_from_aads_auto_apply():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    name = "20260930_obys_employment_audit_lookup.sql"
    sql = (root / "migrations" / name).read_text(encoding="utf-8")
    upper = sql.upper()
    assert "CREATE INDEX IF NOT EXISTS" in upper
    for forbidden in ("UPDATE ", "DELETE ", "DROP ", "TRUNCATE", "INSERT "):
        assert forbidden not in upper
    baseline = (root / "scripts" / "migrations_auto_apply_baseline.txt").read_text(encoding="utf-8").splitlines()
    assert f"migrations/{name}" in baseline
    assert (root / "migrations" / "rollback" / name.replace(".sql", ".down.sql")).is_file()


def test_backfill_script_defaults_to_dry_run(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    # 신규 서명분은 훅이 처리하므로, 백필 대상은 훅 이전 서명분을 흉내 낸다.
    monkeypatch.setattr(svc, "_sync_employment_after_signature", lambda contract, user: {"status": "skipped"})
    contract = sign(monthly_contract())["contract"]
    assert not employee().get("current_employment")

    path = Path(__file__).resolve().parents[2] / "scripts" / "backfill_employment_snapshots.py"
    spec = importlib.util.spec_from_file_location("backfill_employment_snapshots", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main(["--tenant", TENANT]) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and contract["id"] in out
    assert not employee().get("current_employment")

    assert module.main(["--tenant", TENANT, "--apply"]) == 0
    assert employee()["current_employment_contract_id"] == contract["id"]
    assert audit_rows()[0]["details"]["trigger"] == "backfill"
