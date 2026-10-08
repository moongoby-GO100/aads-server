"""ACCT-CONTRACT-AMENDMENT-CHAIN-20261008 — 변경계약: 원계약 연결·이전 계약 표시·시작일 기준 현재 조건.

파일 모드로만 돈다(운영 DB·실제 알림 미접촉).
"""
import base64
import copy
import os
from datetime import date, datetime, timedelta
from io import BytesIO

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, yeoljeong_ops_service

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
OTHER_EMPLOYEE = {"email": "other@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}

ORIGINAL_TERMS = {"start_date": "2026-06-25", "end_date": "2026-07-31", "contract_date": "2026-06-24"}
AMEND_TERMS = {"start_date": "2026-08-01", "end_date": "", "contract_date": "2026-07-30"}


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

    clock = {"now": datetime(2026, 9, 30, 9, 0, tzinfo=svc.KST)}

    def fake_now():
        clock["now"] += timedelta(minutes=1)
        return clock["now"].isoformat(timespec="seconds")

    monkeypatch.setattr(svc, "_now", fake_now)

    async def fake_create_notification(**kwargs):
        return {"id": "ntf-test", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)

    svc._write("employee_join_requests", [
        {"id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"],
         "address": "서울시 직원 주소", "phone": "010-1234-5678", "birth_date": "1990-01-01",
         "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved"},
        {"id": "join-other", "name": "다른 직원", "email": OTHER_EMPLOYEE["email"],
         "address": "서울시 다른 주소", "phone": "010-9999-8888", "birth_date": "1991-02-02",
         "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved"},
        {"id": "join-junghwa", "name": "가입 직원", "email": EMPLOYEE["email"],
         "address": "서울시 직원 주소", "phone": "010-1234-5678", "birth_date": "1990-01-01",
         "tenant_id": TENANT, "business_id": "biz-junghwa", "branch": "중화점", "status": "approved"},
    ])
    return tmp_path


EMPLOYMENT_TERMS = {
    "employment_tax_type": "four_insurance", "job_description": "매장 운영",
    "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
    "work_days": "월~금 주 5일", "holidays": "매주 일요일", "pay_date": "매월 10일",
    "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
    "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
    "insurance_terms": "4대보험 법정 기준 적용",
}


def monthly_contract(wage=3000000, request_id="join-mia", business_id="biz-mia", branch="열정국밥_미아점", **overrides):
    payload = {
        "employee_request_id": request_id, "business_id": business_id, "branch": branch,
        "workplace": "열정국밥 매장", **ORIGINAL_TERMS, **EMPLOYMENT_TERMS,
        "contract_type": "regular", "wage_type": "monthly", "wage": wage,
        "base_salary": wage - 200000, "non_tax_meal_allowance": 200000, "taxable_allowance": 0,
        "meal_provision": "cash_no_meal",
    }
    payload.update(overrides)
    return payload


def _signature_png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def sign(payload, user=EMPLOYEE, name="가입 직원"):
    saved = svc.save_contract(payload, ADMIN)
    requested = svc.request_contract_signature(saved["id"], ADMIN)
    return svc.sign_contract_and_deliver(
        {
            "token": requested["sign_token"],
            "signer_name": name,
            "consent": True,
            "consent_version": "yeoljeong-contract-sign-v1",
            "signature_data_uri": "data:image/png;base64," + base64.b64encode(_signature_png()).decode("ascii"),
        },
        user,
    )


def stored(contract_id):
    return next(row for row in svc._read_hr("contracts", ADMIN) if row["id"] == contract_id)


def employee():
    return next(row for row in svc._read_hr("employee_join_requests", ADMIN) if row["id"] == "join-mia")


def derive(today):
    return svc._derive_current_employment(
        svc._read_hr("contracts", ADMIN), employee_email=EMPLOYEE["email"], employee_request_id="join-mia",
        business_id="biz-mia", today=today,
    )


# --- (a) 서명 순서가 아니라 시작일 기준 -------------------------------------------


def test_amendment_signed_first_still_wins_by_start_date():
    original = svc.save_contract(monthly_contract(), ADMIN)
    amendment = sign(monthly_contract(wage=3200000, amends_contract_id=original["id"], **AMEND_TERMS))["contract"]
    # 변경 계약이 먼저 서명된 시점에는 원계약이 아직 draft 다. 이제 원계약을 나중에 서명한다.
    requested = svc.request_contract_signature(original["id"], ADMIN)
    signed_original = svc.sign_contract_and_deliver(
        {"token": requested["sign_token"], "signer_name": "가입 직원", "consent": True,
         "consent_version": "yeoljeong-contract-sign-v1",
         "signature_data_uri": "data:image/png;base64," + base64.b64encode(_signature_png()).decode("ascii")},
        EMPLOYEE,
    )["contract"]

    assert svc._pg_ts(signed_original["signed_at"]) > svc._pg_ts(amendment["signed_at"])
    derived = derive(date(2026, 10, 8))
    assert derived["contract_id"] == amendment["id"]
    assert derived["employment"]["wage"] == 3200000
    assert employee()["current_employment_contract_id"] == amendment["id"]
    assert employee()["current_employment"]["wage"] == 3200000

    history = svc.employee_employment_history("join-mia", ADMIN)
    assert [item["contract_id"] for item in history["contracts"]] == [original["id"], amendment["id"]]
    assert history["contracts"][0]["superseded_by"] == amendment["id"]
    assert history["contracts"][1]["amends_contract_id"] == original["id"]


def test_original_is_current_while_its_period_covers_today():
    original = sign(monthly_contract())["contract"]
    amendment = sign(monthly_contract(wage=3200000, amends_contract_id=original["id"], **AMEND_TERMS))["contract"]
    assert derive(date(2026, 7, 15))["contract_id"] == original["id"]
    assert derive(date(2026, 7, 31))["contract_id"] == original["id"]
    assert derive(date(2026, 8, 1))["contract_id"] == amendment["id"]


def test_same_start_date_falls_back_to_later_signature():
    first = sign(monthly_contract(start_date="2026-06-25", end_date=""))["contract"]
    second = sign(monthly_contract(wage=3100000, start_date="2026-06-25", end_date=""))["contract"]
    assert derive(date(2026, 10, 8))["contract_id"] == second["id"] != first["id"]


def test_no_active_contract_falls_back_to_latest_start_date():
    sign(monthly_contract())
    later = sign(monthly_contract(wage=3100000, start_date="2026-08-01", end_date="2026-08-31"))["contract"]
    assert derive(date(2026, 12, 1))["contract_id"] == later["id"]
    assert derive(date(2026, 1, 1))["contract_id"] == later["id"]


# --- (b) 미래 시작 계약 ------------------------------------------------------------


def test_future_start_contract_is_not_current_until_start_date():
    current = sign(monthly_contract(start_date="2026-06-25", end_date=""))["contract"]
    future = sign(monthly_contract(wage=3500000, start_date="2099-01-01", end_date=""))["contract"]
    assert derive(date(2026, 10, 8))["contract_id"] == current["id"]
    assert derive(date(2098, 12, 31))["contract_id"] == current["id"]
    assert derive(date(2099, 1, 1))["contract_id"] == future["id"]
    assert employee()["current_employment_contract_id"] == current["id"]


def test_business_scope_is_kept():
    sign(monthly_contract())
    other = sign(monthly_contract(wage=2900000, request_id="join-junghwa", business_id="biz-junghwa", branch="중화점"))["contract"]
    mia = svc._derive_current_employment(
        svc._read_hr("contracts", ADMIN), employee_email=EMPLOYEE["email"], employee_request_id="",
        business_id="biz-junghwa", today=date(2026, 10, 8),
    )
    assert mia["contract_id"] == other["id"]


# --- (c) amends_contract_id 검증 ---------------------------------------------------


def test_amends_self_is_rejected():
    original = svc.save_contract(monthly_contract(), ADMIN)
    with pytest.raises(svc.HTTPException) as excinfo:
        svc.save_contract(monthly_contract(id=original["id"], amends_contract_id=original["id"]), ADMIN)
    assert excinfo.value.status_code == 400
    assert "amends_contract_id" not in stored(original["id"])


def test_amends_other_employee_contract_is_rejected():
    original = svc.save_contract(monthly_contract(), ADMIN)
    with pytest.raises(svc.HTTPException) as excinfo:
        svc.save_contract(
            monthly_contract(request_id="join-other", amends_contract_id=original["id"], **AMEND_TERMS), ADMIN
        )
    assert excinfo.value.status_code == 400
    assert len(svc._read_hr("contracts", ADMIN)) == 1


def test_amends_other_business_contract_is_rejected():
    original = svc.save_contract(monthly_contract(), ADMIN)
    with pytest.raises(svc.HTTPException) as excinfo:
        svc.save_contract(
            monthly_contract(request_id="join-junghwa", business_id="biz-junghwa", branch="중화점",
                             amends_contract_id=original["id"], **AMEND_TERMS),
            ADMIN,
        )
    assert excinfo.value.status_code == 400


def test_amends_missing_or_deleted_contract_is_rejected():
    with pytest.raises(svc.HTTPException) as missing:
        svc.save_contract(monthly_contract(amends_contract_id="no-such-contract"), ADMIN)
    assert missing.value.status_code == 400
    original = svc.save_contract(monthly_contract(), ADMIN)
    rows = svc._read_file_rows("contracts")
    next(row for row in rows if row["id"] == original["id"])["deleted_at"] = "2026-10-01T00:00:00+09:00"
    svc._write_file_rows("contracts", rows)
    with pytest.raises(svc.HTTPException) as deleted:
        svc.save_contract(monthly_contract(amends_contract_id=original["id"], **AMEND_TERMS), ADMIN)
    assert deleted.value.status_code == 400


def test_amendment_cycle_is_rejected():
    original = svc.save_contract(monthly_contract(), ADMIN)
    amendment = svc.save_contract(monthly_contract(wage=3200000, amends_contract_id=original["id"], **AMEND_TERMS), ADMIN)
    with pytest.raises(svc.HTTPException) as excinfo:
        svc.save_contract(monthly_contract(id=original["id"], amends_contract_id=amendment["id"]), ADMIN)
    assert excinfo.value.status_code == 400


def test_camel_case_key_is_normalized_and_clearing_removes_link():
    original = svc.save_contract(monthly_contract(), ADMIN)
    draft = svc.save_contract(monthly_contract(amendsContractId=original["id"], **AMEND_TERMS), ADMIN)
    assert draft["amends_contract_id"] == original["id"] and "amendsContractId" not in draft
    cleared = svc.save_contract(monthly_contract(id=draft["id"], amends_contract_id="", **AMEND_TERMS), ADMIN)
    assert "amends_contract_id" not in cleared


def test_client_cannot_forge_computed_superseded_fields():
    draft = svc.save_contract(monthly_contract(superseded_by="x", superseded_label="이전 계약"), ADMIN)
    assert "superseded_by" not in draft and "superseded_label" not in draft


# --- (d) 원계약 서명본·스냅샷 불변 ----------------------------------------------------


def test_signing_amendment_never_rewrites_signed_original():
    original = sign(monthly_contract())["contract"]
    before = copy.deepcopy(stored(original["id"]))
    listed_before = {row["id"]: row for row in svc.list_contracts(ADMIN)}
    assert "superseded_by" not in listed_before[original["id"]]

    amendment_draft = svc.save_contract(monthly_contract(wage=3200000, amends_contract_id=original["id"], **AMEND_TERMS), ADMIN)
    # 변경 계약이 draft/requested 인 동안은 원계약에 '이전 계약' 표시가 없다.
    assert "superseded_by" not in {row["id"]: row for row in svc.list_contracts(ADMIN)}[original["id"]]
    amendment = sign({**amendment_draft, "id": amendment_draft["id"]})["contract"]

    assert stored(original["id"]) == before
    assert stored(original["id"])["signed_snapshot"] == before["signed_snapshot"]
    assert stored(original["id"])["signed_snapshot_sha256"] == before["signed_snapshot_sha256"]
    assert stored(original["id"])["signed_at"] == before["signed_at"]
    assert "superseded_by" not in stored(original["id"])

    listed = {row["id"]: row for row in svc.list_contracts(ADMIN)}
    assert listed[original["id"]]["superseded_by"] == amendment["id"]
    assert listed[original["id"]]["superseded_label"] == "이전 계약"
    assert listed[amendment["id"]]["amends_contract_id"] == original["id"]
    assert "superseded_by" not in listed[amendment["id"]]
    # 저장소의 서명본은 계산 필드 없이 그대로다.
    assert "superseded_by" not in stored(original["id"])

    with pytest.raises(svc.HTTPException) as edit:
        svc.save_contract({**listed[original["id"]], "wage": 1}, ADMIN)
    assert edit.value.status_code == 409


# --- 화면(정적) ---------------------------------------------------------------------


def test_admin_editor_has_amendment_button_and_labels():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"
    editor = (root / "modules" / "contract-editor-v41.js").read_text(encoding="utf-8")
    assert '"amendsContractId"]' in editor
    assert "data-cv41-amend>" in editor or "data-cv41-amend " in editor
    assert "변경 계약 작성" in editor
    assert 'amendsContractId: String(original.id), contractDate: "", startDate: "", endDate: ""' in editor
    assert "변경 계약(원계약" in editor and "이전 계약" in editor
    assert "modules/contract-editor-v41.js?v=20261008-r1" in (root / "mockup-v4-1.html").read_text(encoding="utf-8")
