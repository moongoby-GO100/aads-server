"""ACCT-OBYS-CONTRACT-BLANK-SIGNATURE-BLOCK-20261016 — 빈 서명·점 하나 서명으로 계약이 서명 완료되는 것을 막는다.

서버: PNG 를 디코딩해 잉크 픽셀 수·바운딩박스 하한을 본다. 클라이언트: 누적 획 길이 하한(정적 검사).
파일 모드로만 돈다(운영 DB·업로드 경로·알리고 미접촉).
"""
import base64
import os
import re
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api
from app.services import aligo_client, yeoljeong_ops_service

svc = api.svc
TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
BLANK_DETAIL = "자필서명이 비어 있거나 너무 짧습니다. 서명을 다시 입력하십시오"
STATIC = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys"


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
    svc._write("employee_join_requests", [{
        "id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"], "address": "서울시 직원 주소",
        "phone": "010-1234-5678", "birth_date": "1990-01-01", "tenant_id": TENANT, "business_id": "biz-mia",
        "branch": "열정국밥_미아점", "status": "approved",
    }])


def _png(image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _uri(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _transparent_blank() -> bytes:
    return _png(Image.new("RGBA", (600, 220), (0, 0, 0, 0)))


def _white_blank() -> bytes:
    return _png(Image.new("RGB", (600, 220), "white"))


def _dot() -> bytes:
    image = Image.new("RGB", (600, 220), "white")
    ImageDraw.Draw(image).ellipse((300, 100, 306, 106), fill="black")
    return _png(image)


def _tiny_stroke() -> bytes:
    image = Image.new("RGB", (600, 220), "white")
    ImageDraw.Draw(image).line((300, 100, 312, 100), fill="black", width=3)
    return _png(image)


def _real_line() -> bytes:
    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    return _png(image)


def _horizontal_line() -> bytes:
    image = Image.new("RGBA", (600, 220), (255, 255, 255, 0))
    ImageDraw.Draw(image).line((100, 110, 260, 110), fill=(19, 35, 42, 255), width=3)
    return _png(image)


def _thresholds_client_stroke_on_server(css_length: float, line_width: float) -> bytes:
    """클라이언트 최소 획 길이(60px)를 막 넘는 가로 획이 서버 하한을 통과하는지 시험하는 이미지."""
    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((20, 60, 20 + css_length, 60), fill="#0f172a", width=round(line_width))
    return _png(image)


def _assert_blank(raw: bytes):
    with pytest.raises(HTTPException) as caught:
        svc._validated_signature_image(_uri(raw))
    assert caught.value.status_code == 400
    assert caught.value.detail == BLANK_DETAIL


# --- 1. 서버 잉크 판정 -----------------------------------------------------------------------
def test_transparent_blank_png_is_rejected_even_though_it_passes_old_length_check():
    raw = _transparent_blank()
    assert 100 <= len(raw) <= svc.MAX_SIGNATURE_BYTES
    _assert_blank(raw)


def test_white_background_blank_png_is_rejected():
    _assert_blank(_white_blank())


def test_single_dot_png_is_rejected():
    _assert_blank(_dot())


def test_too_short_stroke_png_is_rejected():
    _assert_blank(_tiny_stroke())


def test_transparent_black_pixels_do_not_count_as_ink():
    image = Image.new("RGBA", (600, 220), (0, 0, 0, 0))
    image.putpixel((10, 10), (0, 0, 0, 0))
    _assert_blank(_png(image))


@pytest.mark.parametrize("make", [_real_line, _horizontal_line])
def test_real_line_signature_passes_and_returns_hash(make):
    raw = make()
    value, digest = svc._validated_signature_image(_uri(raw))
    assert value == _uri(raw)
    assert len(digest) == 64


def test_client_minimum_stroke_passes_server_floor():
    # index.html 선폭 3(캔버스 px)·employee-portal.js 선폭 2.4(CSS px) 로 최소 획 길이를 막 넘긴 선.
    for width in (2.4, 3):
        value, _digest = svc._validated_signature_image(_uri(_thresholds_client_stroke_on_server(60, width)))
        assert value


def test_oversized_dimensions_are_rejected_before_decoding_pixels():
    huge = _png(Image.new("L", (svc.MAX_SIGNATURE_SIDE_PX + 1, 8), 255))
    with pytest.raises(HTTPException) as caught:
        svc._validated_signature_image(_uri(huge))
    assert caught.value.status_code == 400
    bomb = _png(Image.new("1", (4000, 4000), 1))
    assert len(bomb) <= svc.MAX_SIGNATURE_BYTES
    with pytest.raises(HTTPException) as caught:
        svc._validated_signature_image(_uri(bomb))
    assert caught.value.status_code == 400


def test_png_header_with_garbage_body_is_rejected():
    with pytest.raises(HTTPException) as caught:
        svc._validated_signature_image(_uri(b"\x89PNG\r\n\x1a\n" + b"garbage-signature" * 16))
    assert caught.value.status_code == 400


def test_floor_constants_are_defined():
    assert svc.SIGNATURE_MIN_INK_PIXELS >= 30
    assert svc.SIGNATURE_MIN_INK_BBOX_PX >= 10
    assert svc.SIGNATURE_MIN_INK_BBOX_SHORT_PX >= 1


# --- 2. 단건·묶음 서명 경로 -------------------------------------------------------------------
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


def _requested(**overrides):
    saved = svc.save_contract(_contract_payload(**overrides), ADMIN)
    return svc.request_contract_signature(saved["id"], ADMIN)


def _stored(contract_id):
    return next(row for row in svc._read_hr("contracts", ADMIN) if row["id"] == contract_id)


def _sign_payload(token, raw, *bundle_ids):
    payload = {
        "token": token, "signer_name": "가입 직원", "consent": True,
        "consent_version": "yeoljeong-contract-sign-v1", "signature_data_uri": _uri(raw),
    }
    if bundle_ids:
        payload["bundle_contract_ids"] = list(bundle_ids)
    return payload


def _client(user):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


@pytest.mark.parametrize("make", [_transparent_blank, _white_blank, _dot])
def test_single_sign_path_rejects_blank_and_keeps_contract_unsigned(make):
    requested = _requested()
    with pytest.raises(HTTPException) as caught:
        svc.sign_contract(_sign_payload(requested["sign_token"], make()), EMPLOYEE)
    assert caught.value.status_code == 400
    assert caught.value.detail == BLANK_DETAIL
    assert _stored(requested["id"])["status"] != "signed"


def test_single_sign_http_returns_400_for_blank_and_200_for_real_line():
    requested = _requested()
    client = _client(EMPLOYEE)
    blank = client.post("/yeoljeong-finance/contracts/signing", json=_sign_payload(requested["sign_token"], _transparent_blank()))
    assert blank.status_code == 400
    assert BLANK_DETAIL in blank.text
    assert _stored(requested["id"])["status"] != "signed"
    ok = client.post("/yeoljeong-finance/contracts/signing", json=_sign_payload(requested["sign_token"], _real_line()))
    assert ok.status_code == 200, ok.text
    assert _stored(requested["id"])["status"] == "signed"


@pytest.mark.parametrize("make", [_transparent_blank, _white_blank, _dot])
def test_bundle_sign_path_rejects_blank_and_signs_nothing(make):
    first = _requested(start_date="2026-06-25", end_date="2026-07-31", contract_date="2026-06-25")
    later = _requested(start_date="2026-08-01", contract_date="2026-08-01", wage=3000000)
    with pytest.raises(HTTPException) as caught:
        svc.sign_contract_and_deliver(_sign_payload(later["sign_token"], make(), first["id"], later["id"]), EMPLOYEE)
    assert caught.value.status_code == 400
    assert caught.value.detail == BLANK_DETAIL
    assert _stored(first["id"])["status"] != "signed"
    assert _stored(later["id"])["status"] != "signed"


def test_bundle_sign_path_accepts_real_line():
    first = _requested(start_date="2026-06-25", end_date="2026-07-31", contract_date="2026-06-25")
    later = _requested(start_date="2026-08-01", contract_date="2026-08-01", wage=3000000)
    svc.sign_contract_and_deliver(_sign_payload(later["sign_token"], _real_line(), first["id"], later["id"]), EMPLOYEE)
    assert _stored(first["id"])["status"] == _stored(later["id"])["status"] == "signed"


# --- 3. 클라이언트 최소 획 길이(정적 검사) -----------------------------------------------------
def _constant(source: str, name: str) -> float:
    match = re.search(rf"const {name}\s*=\s*([0-9.]+)\s*;", source)
    assert match, f"{name} 상수가 없다"
    return float(match.group(1))


def test_index_html_requires_minimum_stroke_length():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert _constant(html, "CONTRACT_SIGNATURE_MIN_STROKE_PX") >= 40
    assert _constant(html, "CONTRACT_SIGNATURE_MIN_EXTENT_PX") >= svc.SIGNATURE_MIN_INK_BBOX_PX
    assert "contractSignatureStrokeLength += Math.hypot(" in html
    assert "contractSignatureHasInk = contractSignatureStrokeLength >= CONTRACT_SIGNATURE_MIN_STROKE_PX" in html
    assert "서명을 조금 더 길게 써 주십시오." in html
    clear = html[html.index("function clearContractSignature()"):]
    clear = clear[: clear.index("\n    }\n")]
    assert "contractSignatureStrokeLength = 0" in clear
    move = html[html.index('addEventListener("pointermove"', html.index("contractSignatureCanvas?.addEventListener(\"pointerdown\"")):]
    assert "contractSignatureHasInk = true" not in move[:600]


def test_employee_portal_requires_minimum_stroke_length():
    js = (STATIC / "modules" / "employee-portal.js").read_text(encoding="utf-8")
    assert _constant(js, "SIGNATURE_MIN_STROKE_PX") >= 40
    assert _constant(js, "SIGNATURE_MIN_EXTENT_PX") >= svc.SIGNATURE_MIN_INK_BBOX_PX
    assert "strokeLength += Math.hypot(" in js
    assert "signatureLongEnough(strokeLength, extent)" in js
    assert "서명을 조금 더 길게 써 주십시오." in js
    assert "wired.clearPad = () => {\n        resetStroke();" in js
    assert "if (drew) setInk(canvas.toDataURL" not in js
