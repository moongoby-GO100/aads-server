"""ACCT-OBYS-SEAL-REGISTERED-TRADE-NAME-20261016 — 서명 시점 도장 고정(signed_pdf_seal)은 서버가 만든 값만 믿는다.

파일 모드로만 돈다(운영 DB·업로드 경로 미접촉). 운영 이미지 의존(fastapi) — bash scripts/run_unit_tests.sh 로 실행한다.
"""
import base64
import os
from io import BytesIO

import pytest

pytest.importorskip("fastapi", reason="운영 이미지 의존 — scripts/run_unit_tests.sh 로 실행")

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import yeoljeong_contract_pdf as pdf

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
FORGED = {"kind": "individual", "company_name": "위조상호", "representative": "위조대표"}


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture()
def record(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS", "OBYS_CONTRACT_PDF_FONT_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)
    svc._write(
        "employee_join_requests",
        [{"id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"], "address": "서울시 직원 주소", "phone": "010-1234-5678",
          "birth_date": "1990-01-01", "tenant_id": TENANT, "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved"}],
    )
    live = {"name": "열정국밥 중화점", "entityType": "corporation", "representative": "오윤희", "tradeName": "주식회사 윤희에프엔비"}
    monkeypatch.setattr(svc, "_business_record_for_id", lambda business_id: dict(live))
    return live


def _payload(**overrides):
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
        "employer_name": "열정국밥 중화점", "employer_representative": "오윤희",
    }
    payload.update(overrides)
    return payload


def _sign_payload(token):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return {
        "token": token, "signer_name": "가입 직원", "consent": True, "consent_version": "yeoljeong-contract-sign-v1",
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
    }


def _signed(**overrides):
    saved = svc.save_contract(_payload(**overrides), ADMIN)
    requested = svc.request_contract_signature(saved["id"], ADMIN)
    return svc.sign_contract(_sign_payload(requested["sign_token"]), EMPLOYEE)


def _render_spy(monkeypatch, tmp_path):
    seen = []

    def fake_render(contract, seal=None):
        seen.append(seal)
        return b"%PDF-fake"

    monkeypatch.setattr(pdf, "render_signed_contract_pdf", fake_render)
    monkeypatch.setattr(svc, "_persist_contract_delivery", lambda contract, user: None)
    return seen


def test_save_drops_a_client_supplied_signed_pdf_seal(record):
    saved = svc.save_contract(_payload(signed_pdf_seal=FORGED), ADMIN)
    assert "signed_pdf_seal" not in saved
    again = svc.save_contract(_payload(id=saved["id"], signed_pdf_seal=FORGED), ADMIN)
    assert "signed_pdf_seal" not in again
    assert all("signed_pdf_seal" not in row for row in svc._read_hr("contracts", ADMIN))


def test_signing_replaces_any_planted_seal_with_the_server_computed_one(record, monkeypatch, tmp_path):
    saved = svc.save_contract(_payload(), ADMIN)
    requested = svc.request_contract_signature(saved["id"], ADMIN)
    planted = next(row for row in svc._read_hr("contracts", ADMIN) if row["id"] == requested["id"])
    planted["signed_pdf_seal"] = dict(FORGED)  # 저장 API 를 우회해 DB 행에 들어온 경우까지 서명이 덮어쓴다
    svc._write_hr_record("contracts", planted, ADMIN)

    signed = svc.sign_contract(_sign_payload(requested["sign_token"]), EMPLOYEE)
    assert signed["signed_pdf_seal"] == {"kind": "corporation", "company_name": "주식회사 윤희에프엔비", "representative": "오윤희"}
    assert "signed_pdf_seal" not in signed["signed_snapshot"]
    assert svc._signed_contract_snapshot(signed)[1] == signed["signed_snapshot_sha256"]

    seen = _render_spy(monkeypatch, tmp_path)
    assert svc._store_signed_contract_pdf(signed, None)["status"] == "stored"
    assert seen == [signed["signed_pdf_seal"]]


def test_first_signed_pdf_uses_the_registered_name_not_a_forged_seal(record, monkeypatch, tmp_path):
    seen = _render_spy(monkeypatch, tmp_path)
    signed = _signed(signed_pdf_seal=FORGED)
    assert svc._store_signed_contract_pdf(signed, None)["status"] == "stored"
    assert seen[0]["company_name"] == "주식회사 윤희에프엔비" and seen[0]["representative"] == "오윤희"


def test_later_trade_name_change_does_not_change_a_signed_contracts_pdf_seal(record, monkeypatch, tmp_path):
    seen = _render_spy(monkeypatch, tmp_path)
    signed = _signed()
    record["tradeName"] = "주식회사 새상호"  # 서명 뒤에 등록증 상호를 바꿈
    assert svc._store_signed_contract_pdf(signed, None)["status"] == "stored"
    assert seen[0]["company_name"] == "주식회사 윤희에프엔비"


def test_signed_contract_without_a_frozen_seal_does_not_pick_up_the_current_trade_name(record, monkeypatch, tmp_path):
    seen = _render_spy(monkeypatch, tmp_path)
    legacy = {k: v for k, v in _signed().items() if k != "signed_pdf_seal"}
    assert svc._store_signed_contract_pdf(legacy, None)["status"] == "stored"
    assert seen[0]["company_name"] == "열정국밥 중화점"  # trade_name 도입 전 서명본 — 계약서 값 → 사업장 name 순 그대로
    assert "signed_pdf_seal" not in legacy


def test_signing_without_a_trade_name_freezes_the_contract_employer_name(record):
    record.pop("tradeName")
    signed = _signed()
    assert signed["signed_pdf_seal"]["company_name"] == "열정국밥 중화점"
