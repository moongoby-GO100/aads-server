"""AADS-OBYS-H2-PAYROLL-STATE-RETRY2-20261022 — 급여 상태기계·버전·감사·draft 비노출·근태 연동·교부 멱등.

파일 모드로만 돈다. 운영 DB·외부 발송을 건드리지 않고, 발송 함수는 테스트가 갈아 끼운다.
"""
import base64
import os
from datetime import datetime, timedelta
from io import BytesIO

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, yeoljeong_ops_service
from app.services import yeoljeong_payroll_pdf as payroll_pdf

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
OTHER_EMPLOYEE = {"email": "other@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
OTHER_ADMIN = {
    "email": "boss@other.example.com", "is_admin": True, "tenant_id": OTHER_TENANT,
    "current_membership": {"tenant_id": OTHER_TENANT, "status": "active"},
}
MONTH = "2026-10"


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS",
                 "OBYS_CONTRACT_PDF_FONT_PATH", "OBYS_CONTRACT_SIGN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)
    clock = {"now": datetime(2026, 10, 20, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"]

    monkeypatch.setattr(svc, "_now_dt", fake_now)
    notifications: list[dict] = []

    async def fake_create_notification(**kwargs):
        notifications.append(kwargs)
        return {"id": "ntf-test", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    svc._write("employee_join_requests", [{
        "id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"],
        "address": "서울시 직원 주소", "phone": "010-1234-5678", "birth_date": "1990-01-01",
        "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved",
    }])
    return notifications


def _client(user):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


def audit_actions():
    # 감사 로그는 최신순으로 쌓인다 — 발생 순서로 뒤집어 돌려준다.
    rows = [row for row in svc._read_file_rows(svc.EMPLOYMENT_AUDIT_LOG) if row["resource_type"] == "payroll_statement"]
    return [row["action"] for row in reversed(rows)]


def statement(**overrides):
    payload = {
        "employee_email": EMPLOYEE["email"], "employee_name": "가입 직원", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "payroll_month": MONTH, "gross_pay": 3000000, "tax_withholding": 100000, "insurance_deduction": 200000,
        "other_deduction": 0,
    }
    payload.update(overrides)
    return payload


def rows_of(statement_id):
    return [row for row in svc._read_hr("payroll_statements", ADMIN) if row["id"] == statement_id]


def agree_to_delivery():
    return svc.record_payroll_delivery_consent({"agree": True, "consent_version": svc.PAYROLL_DELIVERY_CONSENT_VERSION}, EMPLOYEE)


# --- P-1/P-2 상태기계·버전 ---------------------------------------------------------


def test_save_is_always_draft_and_server_fields_cannot_be_forged():
    saved = svc.save_payroll(
        statement(status="paid", confirmed_by="evil@example.com", confirmed_at="2020-01-01", version=99,
                  supersedes_id="x", deliveries=[{"status": "sent"}]),
        ADMIN,
    )
    assert saved["status"] == "draft"
    assert saved["version"] == 1
    assert saved["net_pay"] == 2700000
    for forged in ("confirmed_by", "confirmed_at", "supersedes_id", "deliveries"):
        assert forged not in saved


def test_client_confirm_request_is_executed_by_server_transition():
    saved = svc.save_payroll(statement(status="confirmed"), ADMIN)
    assert saved["status"] == "confirmed"
    assert saved["confirmed_by"] == ADMIN["email"]
    assert saved["confirmed_at"]
    assert audit_actions() == ["payroll.created", "payroll.confirmed"]


def test_confirmed_payroll_is_never_overwritten_and_edit_creates_new_version():
    draft = svc.save_payroll(statement(), ADMIN)
    confirmed = svc.confirm_payroll(draft["id"], ADMIN)
    edited = svc.save_payroll({**statement(gross_pay=3500000), "id": confirmed["id"]}, ADMIN)

    assert edited["id"] != confirmed["id"]
    assert edited["version"] == 2
    assert edited["status"] == "draft"
    assert edited["supersedes_id"] == confirmed["id"]
    assert edited["root_id"] == confirmed["id"]
    (original,) = rows_of(confirmed["id"])
    assert original["status"] == "confirmed"
    assert original["gross_pay"] == 3000000
    assert original["net_pay"] == 2700000
    assert "payroll.version_created" in audit_actions()

    again = svc.save_payroll({**statement(gross_pay=3600000), "id": confirmed["id"]}, ADMIN)
    assert again["id"] == edited["id"]
    assert again["version"] == 2
    assert [row["gross_pay"] for row in svc.payroll_versions(confirmed["id"], ADMIN)] == [3000000, 3600000]


def test_confirming_new_version_supersedes_old_one_but_old_stays_viewable():
    draft = svc.save_payroll(statement(), ADMIN)
    v1 = svc.confirm_payroll(draft["id"], ADMIN)
    v2 = svc.confirm_payroll(svc.save_payroll({**statement(gross_pay=3300000), "id": v1["id"]}, ADMIN)["id"], ADMIN)

    (old,) = rows_of(v1["id"])
    assert old["superseded_by"] == v2["id"]
    assert old["gross_pay"] == 3000000
    assert [row["id"] for row in svc.list_payroll(ADMIN)] == [v2["id"]]
    assert {row["id"] for row in svc.list_payroll(ADMIN, include_history=True)} == {v1["id"], v2["id"]}
    assert [row["version"] for row in svc.payroll_versions(v2["id"], ADMIN)] == [1, 2]
    with pytest.raises(HTTPException) as exc:
        svc.save_payroll({**statement(gross_pay=3400000), "id": v1["id"]}, ADMIN)
    assert exc.value.status_code == 409
    assert "payroll.superseded" in audit_actions()


def test_identical_resave_of_confirmed_payroll_does_not_spam_versions():
    confirmed = svc.save_payroll(statement(status="confirmed"), ADMIN)
    echoed = svc.save_payroll({**statement(status="confirmed"), "id": confirmed["id"]}, ADMIN)
    assert echoed["id"] == confirmed["id"]
    assert len(svc.payroll_versions(confirmed["id"], ADMIN)) == 1


def test_pay_only_from_latest_confirmed_and_amounts_stay():
    draft = svc.save_payroll(statement(), ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.pay_payroll(draft["id"], ADMIN)
    assert exc.value.status_code == 409
    svc.confirm_payroll(draft["id"], ADMIN)
    paid = svc.pay_payroll(draft["id"], ADMIN)
    assert paid["status"] == "paid"
    assert paid["paid_by"] == ADMIN["email"]
    assert paid["net_pay"] == 2700000
    with pytest.raises(HTTPException):
        svc.confirm_payroll(draft["id"], ADMIN)
    edited = svc.save_payroll({**statement(gross_pay=3100000), "id": draft["id"]}, ADMIN)
    assert edited["status"] == "draft" and edited["version"] == 2


def test_locked_payroll_cannot_be_deleted_and_rejection_is_audited():
    confirmed = svc.save_payroll(statement(status="confirmed"), ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.delete_payroll(confirmed["id"], ADMIN)
    assert exc.value.status_code == 409
    assert rows_of(confirmed["id"])
    assert "payroll.delete_rejected" in audit_actions()


def test_draft_delete_leaves_snapshot_in_audit_log():
    draft = svc.save_payroll(statement(), ADMIN)
    svc.delete_payroll(draft["id"], ADMIN)
    assert rows_of(draft["id"]) == []
    entry = next(row for row in svc._read_file_rows(svc.EMPLOYMENT_AUDIT_LOG) if row["action"] == "payroll.deleted")
    assert entry["resource_id"] == draft["id"]
    assert entry["details"]["snapshot"]["gross_pay"] == 3000000


def test_state_transitions_are_admin_and_tenant_scoped():
    draft = svc.save_payroll(statement(), ADMIN)
    for call in (svc.confirm_payroll, svc.pay_payroll, svc.delete_payroll, svc.payroll_versions):
        with pytest.raises(HTTPException) as exc:
            call(draft["id"], EMPLOYEE)
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            call(draft["id"], OTHER_ADMIN)
        assert exc.value.status_code in {403, 404}
    assert rows_of(draft["id"])[0]["status"] == "draft"


def test_every_save_confirm_and_version_writes_one_audit_row():
    draft = svc.save_payroll(statement(), ADMIN)
    svc.save_payroll({**statement(gross_pay=3100000), "id": draft["id"]}, ADMIN)
    confirmed = svc.confirm_payroll(draft["id"], ADMIN)
    svc.save_payroll({**statement(gross_pay=3200000), "id": confirmed["id"]}, ADMIN)
    assert audit_actions() == ["payroll.created", "payroll.updated", "payroll.confirmed", "payroll.version_created"]
    updated = next(row for row in svc._read_file_rows(svc.EMPLOYMENT_AUDIT_LOG) if row["action"] == "payroll.updated")
    assert updated["details"]["changes"]["gross_pay"] == {"before": 3000000, "after": 3100000}


# --- P-5 직원 본인 조회 --------------------------------------------------------------


def test_employee_api_never_returns_drafts():
    draft = svc.save_payroll(statement(), ADMIN)
    client = _client(EMPLOYEE)
    assert client.get("/yeoljeong-finance/payroll").json()["statements"] == []
    svc.confirm_payroll(draft["id"], ADMIN)
    listed = client.get("/yeoljeong-finance/payroll").json()["statements"]
    assert [row["id"] for row in listed] == [draft["id"]]
    assert _client(OTHER_EMPLOYEE).get("/yeoljeong-finance/payroll").json()["statements"] == []
    new_draft = svc.save_payroll({**statement(gross_pay=3100000), "id": draft["id"]}, ADMIN)
    listed = client.get("/yeoljeong-finance/payroll").json()["statements"]
    assert [row["id"] for row in listed] == [draft["id"]]
    assert new_draft["id"] not in [row["id"] for row in listed]


# --- P-3 음수·절삭 ---------------------------------------------------------------------


@pytest.mark.parametrize("field", ["gross_pay", "tax_withholding", "insurance_deduction", "other_deduction", "taxable_pay", "non_tax_meal_allowance"])
def test_negative_amounts_are_rejected_with_4xx(field):
    response = _client(ADMIN).post("/yeoljeong-finance/payroll", json=statement(**{field: -1}))
    assert 400 <= response.status_code < 500
    assert svc._read_hr("payroll_statements", ADMIN) == []


def test_deductions_above_gross_are_rejected_instead_of_truncated_to_zero():
    response = _client(ADMIN).post("/yeoljeong-finance/payroll", json=statement(gross_pay=100000, tax_withholding=90000, insurance_deduction=20000))
    assert response.status_code == 400
    assert svc._read_hr("payroll_statements", ADMIN) == []


# --- P-6 period ↔ payroll_month ------------------------------------------------------


def test_form_period_is_stored_as_payroll_month_and_month_filter_finds_it():
    body = statement(status="confirmed")
    body.pop("payroll_month")
    body["period"] = "2026-09"
    saved = _client(ADMIN).post("/yeoljeong-finance/payroll", json=body).json()["statement"]
    assert saved["payroll_month"] == "2026-09"
    found = _client(ADMIN).get("/yeoljeong-finance/payroll", params={"payroll_month": "2026-09"}).json()["statements"]
    assert [row["id"] for row in found] == [saved["id"]]
    assert _client(ADMIN).get("/yeoljeong-finance/payroll", params={"payroll_month": "2026-10"}).json()["statements"] == []
    assert [row["id"] for row in svc.list_payroll(EMPLOYEE, "2026-09")] == [saved["id"]]


def test_conflicting_or_malformed_month_is_rejected():
    for body in (statement(period="2026-09"), statement(payroll_month="2026-13"), statement(payroll_month="abc")):
        response = _client(ADMIN).post("/yeoljeong-finance/payroll", json=body)
        assert response.status_code == 400
    assert svc._read_hr("payroll_statements", ADMIN) == []


# --- P-7 근태 → 급여 초안 ---------------------------------------------------------------


def _signature_png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def sign_contract(**overrides):
    terms = {
        "employee_request_id": "join-mia", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "contract_date": "2026-10-01", "start_date": "2026-10-01", "workplace": "열정국밥 미아점",
        "employment_tax_type": "four_insurance", "job_description": "매장 운영", "work_time": "09:00-18:00",
        "rest_time": "12:00-13:00", "weekly_hours": "주 20시간", "work_days": "월·수·금", "holidays": "매주 일요일",
        "pay_date": "매월 10일", "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용", "contract_type": "part_time", "wage_type": "hourly", "wage": 11000,
        "daily_work_schedule": "월수금 각 6시간 40분", "meal_provision": "employer_meal",
    }
    terms.update(overrides)
    saved = svc.save_contract(terms, ADMIN)
    requested = svc.request_contract_signature(saved["id"], ADMIN)
    return svc.sign_contract_and_deliver(
        {
            "token": requested["sign_token"], "signer_name": "가입 직원", "consent": True,
            "consent_version": "yeoljeong-contract-sign-v1",
            "signature_data_uri": "data:image/png;base64," + base64.b64encode(_signature_png()).decode("ascii"),
        },
        EMPLOYEE,
    )


def fake_attendance(monkeypatch, rows):
    seen = []

    def fake(business_id, employee_email, month):
        seen.append((business_id, employee_email, month))
        return rows

    monkeypatch.setattr(svc, "_payroll_attendance_rows", fake)
    return seen


def attendance(day, minutes, status="approved", wage=11000):
    return {"id": f"att-{day}", "work_date": f"2026-10-{day:02d}", "worked_minutes": minutes, "hourly_wage": wage, "status": status}


def test_confirmed_attendance_becomes_hourly_payroll_draft_with_matching_amount(monkeypatch):
    sign_contract()
    seen = fake_attendance(monkeypatch, [attendance(5, 480), attendance(7, 240), attendance(9, 360, status="rejected")])
    draft = svc.payroll_draft_from_attendance({"employee_email": EMPLOYEE["email"], "payroll_month": MONTH}, ADMIN)

    assert seen == [("biz-mia", EMPLOYEE["email"], MONTH)]
    assert draft["status"] == "draft"
    assert draft["gross_pay"] == 12 * 11000
    assert draft["net_pay"] == 12 * 11000
    assert draft["attendance_basis"]["approved_minutes"] == 720
    assert draft["attendance_basis"]["record_ids"] == ["att-5", "att-7"]
    assert svc.list_payroll(EMPLOYEE) == []
    assert "payroll.draft_from_attendance" in audit_actions()

    rerun = svc.payroll_draft_from_attendance({"employee_email": EMPLOYEE["email"], "payroll_month": MONTH}, ADMIN)
    assert rerun["id"] == draft["id"]
    assert len(svc._read_hr("payroll_statements", ADMIN)) == 1


def test_daily_wage_attendance_counts_distinct_approved_days(monkeypatch):
    sign_contract(contract_type="daily", wage_type="daily", wage=90000, end_date="2026-12-31")
    fake_attendance(monkeypatch, [attendance(5, 300), attendance(5, 200), attendance(6, 480)])
    draft = svc.payroll_draft_from_attendance({"employee_email": EMPLOYEE["email"], "payroll_month": MONTH}, ADMIN)
    assert draft["gross_pay"] == 2 * 90000


def test_unconfirmed_or_missing_attendance_is_rejected(monkeypatch):
    sign_contract()
    request = {"employee_email": EMPLOYEE["email"], "payroll_month": MONTH}
    fake_attendance(monkeypatch, [attendance(5, 480), attendance(6, 480, status="pending")])
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_attendance(request, ADMIN)
    assert exc.value.status_code == 409
    fake_attendance(monkeypatch, [attendance(6, 480, status="rejected")])
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_attendance(request, ADMIN)
    assert exc.value.status_code == 409
    assert svc._read_hr("payroll_statements", ADMIN) == []


def test_attendance_draft_requires_hourly_or_daily_contract_and_unlocked_month(monkeypatch):
    request = {"employee_email": EMPLOYEE["email"], "payroll_month": MONTH}
    fake_attendance(monkeypatch, [attendance(5, 480)])
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_attendance(request, ADMIN)
    assert exc.value.status_code == 409  # 서명 계약 없음
    sign_contract()
    draft = svc.payroll_draft_from_attendance(request, ADMIN)
    svc.confirm_payroll(draft["id"], ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_attendance(request, ADMIN)
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_attendance(request, EMPLOYEE)
    assert exc.value.status_code == 403


def test_attendance_source_unavailable_without_db_is_503():
    sign_contract()
    with pytest.raises(HTTPException) as exc:
        svc.payroll_draft_from_attendance({"employee_email": EMPLOYEE["email"], "payroll_month": MONTH}, ADMIN)
    assert exc.value.status_code == 503


# --- P-4 합계 일치 --------------------------------------------------------------------


def pdf_text(data):
    from pdfminer.high_level import extract_text

    return " ".join(extract_text(BytesIO(data)).split())


def test_pdf_totals_equal_screen_totals():
    saved = svc.save_payroll(statement(status="confirmed"), ADMIN)
    (listed,) = _client(ADMIN).get("/yeoljeong-finance/payroll").json()["statements"]
    screen = listed["totals"]
    assert screen["net_pay"] == listed["net_pay"] == 2700000
    assert screen["total_deductions"] == 300000 and screen["consistent"] is True

    response = _client(EMPLOYEE).get(f"/yeoljeong-finance/payroll/{saved['id']}/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    text = pdf_text(response.content)
    for label, value in payroll_pdf.payroll_pdf_rows(listed):
        assert label in text and value in text
    assert f"{screen['gross_pay']:,}원" in text
    assert f"{screen['total_deductions']:,}원" in text
    assert f"{screen['net_pay']:,}원" in text


def test_pdf_is_hidden_for_drafts_other_employees_and_inconsistent_totals():
    draft = svc.save_payroll(statement(), ADMIN)
    assert _client(EMPLOYEE).get(f"/yeoljeong-finance/payroll/{draft['id']}/pdf").status_code == 404
    svc.confirm_payroll(draft["id"], ADMIN)
    assert _client(OTHER_EMPLOYEE).get(f"/yeoljeong-finance/payroll/{draft['id']}/pdf").status_code == 404
    with pytest.raises(payroll_pdf.PayrollPdfError):
        payroll_pdf.render_payroll_statement_pdf({**rows_of(draft["id"])[0], "net_pay": 1})
    with pytest.raises(payroll_pdf.PayrollPdfError):
        payroll_pdf.render_payroll_statement_pdf({**rows_of(draft["id"])[0], "status": "draft"})


# --- P-14 교부: 동의 게이트 + 멱등 -------------------------------------------------------


@pytest.fixture
def sender(monkeypatch):
    calls = []

    def fake_send(row, digest):
        calls.append((row["id"], digest))
        return {"channel": "test", "status": "sent", "target_masked": "m***@example.com", "error_detail": ""}

    monkeypatch.setattr(svc, "_send_payroll_notice", fake_send)
    return calls


def test_delivery_is_blocked_without_employee_consent_and_for_drafts(sender):
    draft = svc.save_payroll(statement(), ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.deliver_payroll(draft["id"], ADMIN)
    assert exc.value.status_code == 409
    svc.confirm_payroll(draft["id"], ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.deliver_payroll(draft["id"], ADMIN)
    assert exc.value.status_code == 409 and "동의" in exc.value.detail
    assert sender == []
    assert _client(EMPLOYEE).post(f"/yeoljeong-finance/payroll/{draft['id']}/deliver").status_code == 403


def test_consent_requires_current_version_and_employee_identity(sender):
    with pytest.raises(HTTPException) as exc:
        svc.record_payroll_delivery_consent({"agree": True, "consent_version": "old"}, EMPLOYEE)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        svc.record_payroll_delivery_consent({"agree": True, "consent_version": svc.PAYROLL_DELIVERY_CONSENT_VERSION}, OTHER_EMPLOYEE)
    assert exc.value.status_code == 404
    assert agree_to_delivery() == {"agreed": True, "version": svc.PAYROLL_DELIVERY_CONSENT_VERSION}
    assert "payroll.delivery_consent_agreed" in [row["action"] for row in svc._read_file_rows(svc.EMPLOYMENT_AUDIT_LOG)]


def test_duplicate_delivery_calls_send_once_and_resend_is_logged(sender):
    confirmed = svc.save_payroll(statement(status="confirmed"), ADMIN)
    agree_to_delivery()

    first = svc.deliver_payroll(confirmed["id"], ADMIN)
    second = svc.deliver_payroll(confirmed["id"], ADMIN)
    assert len(sender) == 1
    assert first["status"] == "sent" and first["duplicate"] is False
    assert second["duplicate"] is True and second["delivery_count"] == 1
    assert len(rows_of(confirmed["id"])[0]["deliveries"]) == 1

    resent = svc.deliver_payroll(confirmed["id"], ADMIN, resend=True)
    assert len(sender) == 2
    log = rows_of(confirmed["id"])[0]["deliveries"]
    assert [item["kind"] for item in log] == ["initial", "resend"]
    assert log[0]["idempotency_key"] != log[1]["idempotency_key"]
    assert resent["delivery_count"] == 2
    assert log[0]["pdf_sha256"] == log[1]["pdf_sha256"]
    assert log[0]["net_pay"] == 2700000
    assert audit_actions().count("payroll.delivered") == 2


def test_resend_before_initial_and_failed_send_retry(sender, monkeypatch):
    confirmed = svc.save_payroll(statement(status="confirmed"), ADMIN)
    agree_to_delivery()
    with pytest.raises(HTTPException) as exc:
        svc.deliver_payroll(confirmed["id"], ADMIN, resend=True)
    assert exc.value.status_code == 409

    monkeypatch.setattr(svc, "_send_payroll_notice", lambda row, digest: {"channel": "test", "status": "failed", "target_masked": "", "error_detail": "boom"})
    failed = svc.deliver_payroll(confirmed["id"], ADMIN)
    assert failed["status"] == "failed"
    monkeypatch.setattr(svc, "_send_payroll_notice", lambda row, digest: (sender.append(row["id"]) or {"channel": "test", "status": "sent", "target_masked": "", "error_detail": ""}))
    retried = svc.deliver_payroll(confirmed["id"], ADMIN)
    assert retried["status"] == "sent" and retried["duplicate"] is False
    assert len(sender) == 1
    assert "payroll.delivery_failed" in audit_actions()


def test_revoked_consent_blocks_delivery_and_new_version_is_deliverable_separately(sender):
    confirmed = svc.save_payroll(statement(status="confirmed"), ADMIN)
    agree_to_delivery()
    svc.record_payroll_delivery_consent({"agree": False, "consent_version": svc.PAYROLL_DELIVERY_CONSENT_VERSION}, EMPLOYEE)
    with pytest.raises(HTTPException):
        svc.deliver_payroll(confirmed["id"], ADMIN)
    agree_to_delivery()
    svc.deliver_payroll(confirmed["id"], ADMIN)
    v2 = svc.confirm_payroll(svc.save_payroll({**statement(gross_pay=3100000), "id": confirmed["id"]}, ADMIN)["id"], ADMIN)
    with pytest.raises(HTTPException):
        svc.deliver_payroll(confirmed["id"], ADMIN, resend=True)  # 대체된 버전은 보내지 않는다
    svc.deliver_payroll(v2["id"], ADMIN)
    assert len(sender) == 2


def test_default_sender_uses_only_in_app_notification_once(isolated):
    notifications = isolated
    confirmed = svc.save_payroll(statement(status="confirmed"), ADMIN)
    agree_to_delivery()
    svc.deliver_payroll(confirmed["id"], ADMIN)
    svc.deliver_payroll(confirmed["id"], ADMIN)
    assert len(notifications) == 1
    assert notifications[0]["notification_type"] == "payroll_statement_ready"
    assert notifications[0]["target_user"] == EMPLOYEE["email"]
    assert rows_of(confirmed["id"])[0]["deliveries"][0]["channel"] == "inapp"


def test_delivery_requires_linked_employee_account(sender):
    confirmed = svc.save_payroll(statement(employee_email="", employee_name="미연결 직원", status="confirmed"), ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.deliver_payroll(confirmed["id"], ADMIN)
    assert exc.value.status_code == 409
    assert sender == []
