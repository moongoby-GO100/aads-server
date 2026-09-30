"""AADS-OBYS-CONTRACT-NOTIFY-PDF-20260930 — 서명요청 알림 + 서명본 PDF 보관·교부."""
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

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
EMPLOYEE_PHONE = "010-1234-5678"


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """파일 모드로만 돈다 — 운영 DB·운영 업로드 경로·실제 알리고를 절대 건드리지 않는다."""
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS",
                 "OBYS_CONTRACT_ALIMTALK_TEMPLATE_SIGN_REQUEST", "OBYS_CONTRACT_ALIMTALK_TEMPLATE_SIGNED",
                 "OBYS_CONTRACT_PDF_FONT_PATH", "OBYS_CONTRACT_SIGN_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)

    inapp_calls = []

    async def fake_create_notification(**kwargs):
        inapp_calls.append(kwargs)
        return {"id": f"ntf-{len(inapp_calls)}", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)

    aligo_calls = {"sms": [], "alimtalk": []}

    async def fake_send_sms(receiver, msg, sender=None, **kwargs):
        aligo_calls["sms"].append(receiver)
        return {"result_code": 1, "message": "success"}

    async def fake_send_alimtalk(receiver, template_code, message, **kwargs):
        aligo_calls["alimtalk"].append((receiver, template_code))
        return {"code": 0, "message": "성공"}

    monkeypatch.setattr(aligo_client, "is_available", lambda: True)
    monkeypatch.setattr(aligo_client, "send_sms", fake_send_sms)
    monkeypatch.setattr(aligo_client, "send_alimtalk", fake_send_alimtalk)

    svc._write("employee_join_requests", [{
        "id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"],
        "address": "서울시 직원 주소", "phone": EMPLOYEE_PHONE, "birth_date": "1990-01-01",
        "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved",
    }])
    return {"inapp": inapp_calls, "aligo": aligo_calls, "tmp": tmp_path}


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


def _real_signature_png() -> bytes:
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
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(_real_signature_png()).decode("ascii"),
        "audit_ip": "203.0.113.10",
        "audit_user_agent": "pytest",
    }


def _client(user):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


def _history():
    return svc._read_file_rows(svc.CONTRACT_NOTIFICATION_LOG)


def _requested_contract():
    saved = svc.save_contract(_contract_payload(), ADMIN)
    return svc.request_contract_signature_with_notice(saved["id"], ADMIN)


def _signed_contract():
    requested = _requested_contract()["contract"]
    return svc.sign_contract_and_deliver(_sign_payload(requested["sign_token"]), EMPLOYEE)


# --- A. 서명 요청 알림 ------------------------------------------------------


def test_signature_request_records_history_per_channel(isolated):
    result = _requested_contract()
    contract, notify = result["contract"], result["notify"]

    assert contract["status"] == "requested"
    assert notify["status"] == "sent"
    by_channel = {item["channel"]: item for item in notify["channels"]}
    assert by_channel["inapp"]["status"] == "sent"
    assert by_channel["sms"]["status"] == "skipped"
    assert by_channel["alimtalk"]["status"] == "skipped"

    assert len(isolated["inapp"]) == 1
    call = isolated["inapp"][0]
    assert call["notification_type"] == "contract_signature_requested"
    assert call["reference_type"] == "contract"
    assert call["reference_id"] == contract["id"]
    assert call["target_user"] == EMPLOYEE["email"]
    assert call["business_id"] == "biz-mia"

    rows = _history()
    assert {(row["channel"], row["status"]) for row in rows} == {("inapp", "sent"), ("sms", "skipped"), ("alimtalk", "skipped")}
    assert all(row["event"] == "signature_requested" and row["tenant_id"] == TENANT for row in rows)
    assert all(row["contract_id"] == contract["id"] for row in rows)
    # 수신 원문은 이력에 남지 않는다.
    stored = json.dumps(rows, ensure_ascii=False)
    assert EMPLOYEE["email"] not in stored
    assert "01012345678" not in stored and EMPLOYEE_PHONE not in stored


def test_default_channel_setting_never_calls_aligo(isolated, monkeypatch):
    # 템플릿이 설정돼 있어도 채널 opt-in 이 없으면 외부 발송은 0회다.
    monkeypatch.setenv("OBYS_CONTRACT_ALIMTALK_TEMPLATE_SIGN_REQUEST", "TPL_SIGN_REQ")
    monkeypatch.setenv("OBYS_CONTRACT_ALIMTALK_TEMPLATE_SIGNED", "TPL_SIGNED")
    requested = _requested_contract()["contract"]
    svc.sign_contract_and_deliver(_sign_payload(requested["sign_token"]), EMPLOYEE)

    assert isolated["aligo"] == {"sms": [], "alimtalk": []}
    skipped = [row for row in _history() if row["channel"] in {"sms", "alimtalk"}]
    assert skipped and all(row["status"] == "skipped" and row["error_detail"] == "channel_disabled" for row in skipped)


def test_opt_in_sms_sends_to_employee_phone_and_stores_masked_target(isolated, monkeypatch):
    monkeypatch.setenv("OBYS_CONTRACT_NOTIFY_CHANNELS", "inapp,sms,alimtalk")
    notify = _requested_contract()["notify"]

    assert isolated["aligo"]["sms"] == ["01012345678"]
    # 알림톡은 템플릿 코드가 없으면 시도하지 않는다.
    assert isolated["aligo"]["alimtalk"] == []
    by_channel = {item["channel"]: item for item in notify["channels"]}
    assert by_channel["sms"] == {"channel": "sms", "status": "sent", "target_masked": "010-****-5678", "error_detail": ""}
    assert by_channel["alimtalk"]["status"] == "skipped"
    assert by_channel["alimtalk"]["error_detail"].startswith("template_not_configured")
    assert "01012345678" not in json.dumps(_history(), ensure_ascii=False)


def test_notification_failure_does_not_fail_signature_request_api(isolated, monkeypatch):
    async def broken_notification(**kwargs):
        raise ConnectionError("notification db down")

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", broken_notification)
    saved = svc.save_contract(_contract_payload(), ADMIN)

    response = _client(ADMIN).post(f"/yeoljeong-finance/contracts/{saved['id']}/request-signature")

    assert response.status_code == 200
    body = response.json()
    assert body["contract"]["status"] == "requested"
    assert body["contract"]["sign_token"]
    assert body["notify"]["status"] == "failed"
    inapp = [row for row in _history() if row["channel"] == "inapp"]
    assert inapp and inapp[0]["status"] == "failed"
    assert "notification db down" in inapp[0]["error_detail"]


def test_dispatch_crash_is_recorded_and_request_still_succeeds(isolated, monkeypatch):
    from app.services import yeoljeong_contract_notify

    async def crash(contract, event):
        raise RuntimeError("dispatcher exploded")

    monkeypatch.setattr(yeoljeong_contract_notify, "dispatch", crash)
    result = _requested_contract()

    assert result["contract"]["status"] == "requested"
    assert result["notify"]["status"] == "failed"
    assert _history()[0]["status"] == "failed"


def test_resend_signature_notice_rate_limit_and_guards(isolated):
    contract = _requested_contract()["contract"]
    client = _client(ADMIN)
    url = f"/yeoljeong-finance/contracts/{contract['id']}/resend-signature-notice"

    limited = client.post(url)
    assert limited.status_code == 429
    assert len(isolated["inapp"]) == 1

    # 최근 이력을 6분 전으로 돌리면 다시 보낼 수 있다.
    old = (datetime.now(svc.KST) - timedelta(minutes=6)).isoformat(timespec="seconds")
    svc._write_file_rows(svc.CONTRACT_NOTIFICATION_LOG, [{**row, "created_at": old} for row in _history()])
    resent = client.post(url)
    assert resent.status_code == 200
    assert resent.json()["notify"]["status"] == "sent"
    assert len(isolated["inapp"]) == 2
    assert client.post(url).status_code == 429

    assert _client(EMPLOYEE).post(url).status_code == 403

    draft = svc.save_contract(_contract_payload(), ADMIN)
    assert client.post(f"/yeoljeong-finance/contracts/{draft['id']}/resend-signature-notice").status_code == 409


# --- B. 서명본 PDF 보관·교부 ------------------------------------------------


def test_signing_stores_pdf_with_sha256_and_size(isolated):
    pytest.importorskip("reportlab")
    result = _signed_contract()
    contract = result["contract"]

    assert contract["status"] == "signed"
    assert result["signed_pdf"]["status"] == "stored"
    path = Path(os.environ["OBYS_UPLOAD_ROOT"]) / contract["signed_pdf_path"]
    data = path.read_bytes()
    assert data.startswith(b"%PDF-")
    assert contract["signed_pdf_sha256"] == hashlib.sha256(data).hexdigest() == result["signed_pdf"]["sha256"]
    assert contract["signed_pdf_bytes"] == len(data) == result["signed_pdf"]["bytes"]
    assert contract["signed_pdf_path"].startswith(f"{TENANT}/contracts/")
    assert contract["signed_pdf_error"] == ""
    # 저장된 레코드에도 메타가 남는다.
    stored = svc._find(svc._read_hr("contracts", ADMIN), contract["id"])
    assert stored["signed_pdf_sha256"] == contract["signed_pdf_sha256"]
    # 보관·교부 메타는 봉인 스냅샷에 섞이지 않는다.
    assert "signed_pdf_sha256" not in contract["signed_snapshot"]
    # 서명 완료 알림도 같은 경로로 남는다.
    assert isolated["inapp"][-1]["notification_type"] == "contract_signed"
    assert any(row["event"] == "signed" and row["channel"] == "inapp" for row in _history())


def test_pdf_is_bound_to_signed_snapshot_not_current_body(isolated):
    pytest.importorskip("reportlab")
    contract = _signed_contract()["contract"]
    original_sha = contract["signed_pdf_sha256"]
    original_bytes = (Path(os.environ["OBYS_UPLOAD_ROOT"]) / contract["signed_pdf_path"]).read_bytes()

    # 서명 뒤 저장소의 본문을 직접 바꾸고 PDF 파일도 지운다(재생성 강제).
    rows = svc._read_file_rows("contracts")
    for row in rows:
        if row["id"] == contract["id"]:
            row["job_description"] = "서명 후 몰래 바꾼 업무"
            row["wage"] = 1
    svc._write_file_rows("contracts", rows)
    (Path(os.environ["OBYS_UPLOAD_ROOT"]) / contract["signed_pdf_path"]).unlink()

    regenerated = _client(ADMIN).post(f"/yeoljeong-finance/contracts/{contract['id']}/signed-pdf/regenerate")
    assert regenerated.status_code == 200
    assert regenerated.json()["signed_pdf"]["sha256"] == original_sha

    download = _client(ADMIN).get(f"/yeoljeong-finance/contracts/{contract['id']}/signed-pdf")
    assert download.status_code == 200
    assert download.content == original_bytes


def test_pdf_generation_failure_keeps_signature_and_records_error(isolated, monkeypatch):
    monkeypatch.setenv("OBYS_CONTRACT_PDF_FONT_PATH", str(isolated["tmp"] / "missing-font.ttf"))
    result = _signed_contract()
    contract = result["contract"]

    assert contract["status"] == "signed"
    assert contract["signed_snapshot_sha256"]
    assert result["signed_pdf"]["status"] == "failed"
    assert contract["signed_pdf_error"]
    assert not contract.get("signed_pdf_path")
    assert not list((isolated["tmp"] / "upload-root").rglob("*.pdf"))
    stored = svc._find(svc._read_hr("contracts", ADMIN), contract["id"])
    assert stored["status"] == "signed" and stored["signed_pdf_error"]


def test_signed_pdf_download_access_and_delivery_record(isolated):
    pytest.importorskip("reportlab")
    contract = _signed_contract()["contract"]
    url = f"/yeoljeong-finance/contracts/{contract['id']}/signed-pdf"

    assert _client(THIRD_PARTY).get(url).status_code == 403
    assert _client(OTHER_TENANT_ADMIN).get(url).status_code == 403
    # 없는 계약서도 같은 403 — 다른 테넌트에 존재하는지 드러내지 않는다.
    assert _client(OTHER_TENANT_ADMIN).get("/yeoljeong-finance/contracts/no-such/signed-pdf").status_code == 403

    admin_download = _client(ADMIN).get(url)
    assert admin_download.status_code == 200
    assert admin_download.content.startswith(b"%PDF-")
    assert admin_download.headers["content-type"] == "application/pdf"
    assert not svc._find(svc._read_hr("contracts", ADMIN), contract["id"]).get("delivered_at")

    party_download = _client(EMPLOYEE).get(url)
    assert party_download.status_code == 200
    assert hashlib.sha256(party_download.content).hexdigest() == contract["signed_pdf_sha256"]
    stored = svc._find(svc._read_hr("contracts", ADMIN), contract["id"])
    assert stored["delivered_at"]
    assert stored["delivery_channel"] == "download"
    delivered = [row for row in _history() if row["event"] == "delivered"]
    assert delivered and delivered[0]["channel"] == "download" and delivered[0]["status"] == "sent"
    assert EMPLOYEE["email"] not in delivered[0]["target_masked"]


def test_unsigned_contract_pdf_download_is_conflict(isolated):
    contract = _requested_contract()["contract"]
    assert _client(ADMIN).get(f"/yeoljeong-finance/contracts/{contract['id']}/signed-pdf").status_code == 409


def test_signed_contract_stays_immutable(isolated):
    contract = _signed_contract()["contract"]
    with pytest.raises(svc.HTTPException) as edit:
        svc.save_contract({**contract, "wage": 1}, ADMIN)
    assert edit.value.status_code == 409
    with pytest.raises(svc.HTTPException) as delete:
        svc.delete_contract(contract["id"], ADMIN)
    assert delete.value.status_code == 409


def test_migration_is_schema_only_and_held_from_aads_auto_apply():
    root = Path(__file__).resolve().parents[2]
    sql = (root / "migrations" / "20260930_obys_contract_notify_signed_pdf.sql").read_text(encoding="utf-8")
    upper = sql.upper()
    assert "CREATE TABLE IF NOT EXISTS YEOLJEONG_CONTRACT_NOTIFICATIONS" in upper
    assert "CHECK (STATUS IN ('SENT', 'FAILED', 'SKIPPED'))" in upper
    for column in ("signed_pdf_path", "signed_pdf_sha256", "signed_pdf_bytes", "delivered_at", "delivery_channel"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql
    for forbidden in ("UPDATE ", "DELETE ", "DROP ", "TRUNCATE"):
        assert forbidden not in upper
    baseline = (root / "scripts" / "migrations_auto_apply_baseline.txt").read_text(encoding="utf-8").splitlines()
    assert "migrations/20260930_obys_contract_notify_signed_pdf.sql" in baseline
    assert (root / "migrations" / "rollback" / "20260930_obys_contract_notify_signed_pdf.down.sql").is_file()


def test_reportlab_is_pinned_in_runtime_lock():
    root = Path(__file__).resolve().parents[2]
    lock = (root / "requirements.runtime.lock").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("reportlab==") for line in lock)
    assert (root / "app" / "assets" / "fonts" / "NanumGothic-Regular.ttf").is_file()
