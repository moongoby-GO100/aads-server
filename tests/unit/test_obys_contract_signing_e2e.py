"""ACCT-EMPLOYEE-CONTRACT-SIGN-E2E-20261006-R1 — 가입→승인→계약→서명요청→자필서명→서명본 PDF 의 막힘·경계 고정.

파일 모드로만 돈다(운영 DB·업로드 경로·알리고 미접촉). 테넌트 경계·서명 규칙은 느슨하게 하지 않고,
만료·재제출·승인 취소 같은 빠져 있던 경계를 추가로 고정한다.
"""
import base64
import os
import re
from datetime import timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, yeoljeong_ops_service

svc = api.svc
STATIC_HTML = Path(__file__).resolve().parents[2] / "app/static/apps/obys/index.html"
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
OTHER_EMPLOYEE = {"email": "other@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
OTHER_TENANT_EMPLOYEE = {
    "email": EMPLOYEE["email"],
    "is_admin": False,
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
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS",
                 "OBYS_CONTRACT_PDF_FONT_PATH", "OBYS_CONTRACT_SIGN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)

    async def fake_create_notification(**kwargs):
        return {"id": "ntf", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    _write_join("approved")


def _write_join(status):
    svc._write("employee_join_requests", [{
        "id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"],
        "address": "서울시 직원 주소", "phone": "010-1234-5678", "birth_date": "1990-01-01",
        "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": status,
    }])


def _contract_payload(**overrides):
    payload = {
        "employee_request_id": "join-mia", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "contract_type": "regular", "employment_tax_type": "four_insurance",
        "start_date": "2026-07-22", "contract_date": "2026-07-22", "wage_type": "monthly",
        "wage": 2800000, "workplace": "열정국밥 미아점", "job_description": "매장 운영",
        "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
        "work_days": "주 5일", "holidays": "매주 일요일", "pay_date": "매월 10일",
        "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용",
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


def _sign_payload(token):
    return {
        "token": token,
        "signer_name": "가입 직원",
        "consent": True,
        "consent_version": "yeoljeong-contract-sign-v1",
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(_signature_png()).decode("ascii"),
    }


def _client(user):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


def _requested():
    saved = svc.save_contract(_contract_payload(), ADMIN)
    return svc.request_contract_signature(saved["id"], ADMIN)


def _age_request(contract_id, days):
    rows = svc._read_hr("contracts", ADMIN)
    row = next(item for item in rows if item["id"] == contract_id)
    row["requested_at"] = (svc._pg_ts(row["requested_at"]) - timedelta(days=days)).isoformat(timespec="seconds")
    svc._write_hr_record("contracts", row, ADMIN)


# --- 1. 전체 흐름: 승인된 직원 → 계약 → 서명요청 → 자필서명 → 서명본 PDF ---------------
def test_full_flow_ends_with_downloadable_signed_pdf_and_audit_trail():
    contract = _requested()
    token = contract["sign_token"]

    shown = _client(EMPLOYEE).get(f"/yeoljeong-finance/contracts/signing/{token}")
    assert shown.status_code == 200
    assert shown.json()["contract"]["status"] == "requested"

    signed = _client(EMPLOYEE).post("/yeoljeong-finance/contracts/signing", json=_sign_payload(token))
    assert signed.status_code == 200, signed.text
    body = signed.json()
    assert body["contract"]["status"] == "signed"
    assert body["signed_pdf"]["status"] == "stored"
    saved = body["contract"]
    assert saved["signature_audit"]["authenticated_email"] == EMPLOYEE["email"]
    assert saved["signature_audit"]["client_ip"]
    assert saved["signature_consent"]["accepted"] is True
    assert saved["signed_snapshot_sha256"] and saved["signature_sha256"]
    assert "sign_token" not in saved

    for user in (EMPLOYEE, ADMIN):
        pdf = _client(user).get(f"/yeoljeong-finance/contracts/{saved['id']}/signed-pdf")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content.startswith(b"%PDF")
        assert pdf.headers["cache-control"] == "no-store"


# --- 2. 같은 링크 재제출·중복 제출은 403 이 아니라 409 로 "이미 서명" 을 알리고 서명본은 그대로다 ----------
def test_second_submit_with_same_link_is_conflict_and_keeps_original_signature():
    token = _requested()["sign_token"]
    first = svc.sign_contract_and_deliver(_sign_payload(token), EMPLOYEE)["contract"]
    digest = first["signed_snapshot_sha256"]

    with pytest.raises(HTTPException) as again:
        svc.sign_contract_and_deliver({**_sign_payload(token), "signer_name": "가입 직원"}, EMPLOYEE)
    assert again.value.status_code == 409
    assert "이미 서명" in again.value.detail

    http = _client(EMPLOYEE).post("/yeoljeong-finance/contracts/signing", json=_sign_payload(token))
    assert http.status_code == 409
    view = _client(EMPLOYEE).get(f"/yeoljeong-finance/contracts/signing/{token}")
    assert view.status_code == 409

    stored = next(row for row in svc._read_hr("contracts", ADMIN) if row["id"] == first["id"])
    assert stored["signed_snapshot_sha256"] == digest
    assert stored["signed_at"] == first["signed_at"]


def test_signed_link_does_not_leak_to_other_employee_or_other_tenant():
    token = _requested()["sign_token"]
    svc.sign_contract_and_deliver(_sign_payload(token), EMPLOYEE)

    with pytest.raises(HTTPException) as other_employee:
        svc.sign_contract_and_deliver(_sign_payload(token), OTHER_EMPLOYEE)
    assert other_employee.value.status_code == 403

    cross = _client(OTHER_TENANT_EMPLOYEE).post("/yeoljeong-finance/contracts/signing", json=_sign_payload(token))
    assert cross.status_code == 403


# --- 3. 서명 링크 만료 -----------------------------------------------------------------
def test_expired_sign_link_is_gone_and_reissue_revives_it():
    contract = _requested()
    _age_request(contract["id"], svc.CONTRACT_SIGN_LINK_TTL_DAYS + 1)

    with pytest.raises(HTTPException) as shown:
        svc.get_contract_by_token(contract["sign_token"], EMPLOYEE)
    assert shown.value.status_code == 410
    assert "만료" in shown.value.detail
    with pytest.raises(HTTPException) as signed:
        svc.sign_contract(_sign_payload(contract["sign_token"]), EMPLOYEE)
    assert signed.value.status_code == 410

    reissued = svc.request_contract_signature(contract["id"], ADMIN)
    assert svc.get_contract_by_token(reissued["sign_token"], EMPLOYEE)["status"] == "requested"
    assert svc.sign_contract(_sign_payload(reissued["sign_token"]), EMPLOYEE)["status"] == "signed"


def test_link_within_ttl_still_signs():
    contract = _requested()
    _age_request(contract["id"], svc.CONTRACT_SIGN_LINK_TTL_DAYS - 1)
    assert svc.sign_contract(_sign_payload(contract["sign_token"]), EMPLOYEE)["status"] == "signed"


# --- 4. 승인 전·승인 취소·로그인 없음·관리자 대리 서명 차단 -------------------------------------
@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_employee_without_current_approval_cannot_open_or_sign(status):
    contract = _requested()
    _write_join(status)
    with pytest.raises(HTTPException) as shown:
        svc.get_contract_by_token(contract["sign_token"], EMPLOYEE)
    assert shown.value.status_code == 403
    with pytest.raises(HTTPException) as signed:
        svc.sign_contract(_sign_payload(contract["sign_token"]), EMPLOYEE)
    assert signed.value.status_code == 403
    stored = next(row for row in svc._read_hr("contracts", ADMIN) if row["id"] == contract["id"])
    assert stored["status"] == "requested"


def test_contract_cannot_be_drafted_for_unapproved_employee():
    _write_join("pending")
    with pytest.raises(HTTPException) as exc:
        svc.save_contract(_contract_payload(), ADMIN)
    assert exc.value.status_code == 400


def test_anonymous_and_admin_cannot_sign_for_employee():
    token = _requested()["sign_token"]
    with pytest.raises(HTTPException) as anonymous:
        svc.sign_contract(_sign_payload(token), None)
    assert anonymous.value.status_code in {401, 403}
    with pytest.raises(HTTPException) as admin:
        svc.sign_contract(_sign_payload(token), ADMIN)
    assert admin.value.status_code == 403


# --- 5. 다른 직원·다른 회사의 서명본 PDF 접근 차단 ------------------------------------------
def test_signed_pdf_is_private_to_party_and_admin_of_same_tenant():
    token = _requested()["sign_token"]
    signed = svc.sign_contract_and_deliver(_sign_payload(token), EMPLOYEE)["contract"]
    url = f"/yeoljeong-finance/contracts/{signed['id']}/signed-pdf"
    assert _client(OTHER_EMPLOYEE).get(url).status_code == 403
    assert _client(OTHER_TENANT_EMPLOYEE).get(url).status_code == 403


def test_signed_record_and_pdf_survive_regeneration_with_same_sealed_snapshot():
    token = _requested()["sign_token"]
    signed = svc.sign_contract_and_deliver(_sign_payload(token), EMPLOYEE)["contract"]
    regen = _client(ADMIN).post(f"/yeoljeong-finance/contracts/{signed['id']}/signed-pdf/regenerate")
    assert regen.status_code == 200
    stored = next(row for row in svc._read_hr("contracts", ADMIN) if row["id"] == signed["id"])
    assert stored["signed_snapshot_sha256"] == signed["signed_snapshot_sha256"]
    assert stored["signature_audit"] == signed["signature_audit"]
    with pytest.raises(HTTPException) as edit:
        svc.save_contract(_contract_payload(id=signed["id"]), ADMIN)
    assert edit.value.status_code == 409


# --- 6. 화면(정적): 실명·회사·점포 입력, 서명본 PDF 버튼, 세션 만료 복구 -------------------------
def _html():
    return STATIC_HTML.read_text(encoding="utf-8")


def _form_block(html, form_id):
    start = html.index(f'<form id="{form_id}"')
    return html[start:html.index("</form>", start)]


@pytest.mark.parametrize("form_id", ["gateSignupForm", "signupForm"])
def test_employee_signup_forms_show_real_name_company_and_store(form_id):
    block = _form_block(_html(), form_id)
    assert re.search(r'<input name="name" type="text"[^>]*required', block), "실명 입력칸이 보여야 한다"
    assert 'name="name" type="hidden"' not in block
    assert re.search(r'<select name="businessId" required>', block), "회사 선택이 있어야 한다"
    assert re.search(r'<select name="branch"[^>]*>', block), "점포 선택이 있어야 한다"
    assert "form.branch.required = names.length > 0" in _html(), "매장형 사업자는 점포 선택이 필수로 켜져야 한다"


def test_invite_accept_form_requires_real_name():
    block = _form_block(_html(), "gateInviteAcceptForm")
    assert re.search(r'<input name="name" type="text"[^>]*required', block)


def test_signup_sends_business_id_and_recovers_from_partial_failure():
    html = _html()
    assert "business_id: businessId || \"\"" in html
    assert "회사(사업자)를 선택해 주십시오." in html
    assert "계정은 만들어졌지만 가입요청 전송에 실패했습니다" in html
    assert "이미 가입된 이메일입니다. 로그인해서 가입요청 상태를 확인하십시오." in html
    assert "syncSignupBranchSelects" in html


def test_contract_ui_offers_signed_pdf_download_and_expired_session_recovery():
    html = _html()
    assert html.count("data-download-contract-pdf=") >= 2
    assert "/signed-pdf`" in html
    assert "handleExpiredServerSession" in html
    assert "서명은 아직 제출되지 않았습니다" in html
