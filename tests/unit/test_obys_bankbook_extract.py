"""ACCT-OBYS-BANKBOOK-AUTO-EXTRACT-20261009 — 통장사본 자동 판독.

파일 모드로만 돈다(운영 DB 미접촉). OCR 은 가짜 함수로 대체해 실제 tesseract/PC Agent 를 부르지 않는다.
"""
import io
import json
import logging
import os
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.services import obys_bankbook_extract as bb
from app.services import yeoljeong_finance_service as service

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
OWNER = {
    "email": "owner@example.com", "tenant_id": TENANT,
    "current_membership": {"tenant_id": TENANT, "status": "active", "role": "owner"},
}
EMP = "emp-mia@example.com"

RAW_WOORI = "1002-123-456846"
RAW_IBK = "348-123456-01-018"
WOORI_TEXT = f"우리은행\n예금주 : 김민우\n계좌번호 {RAW_WOORI}\n"
IBK_TEXT = f"IBK 기업은행\n예 금 주 홍석빈\n계좌번호 {RAW_IBK}\n"
RAW_DIGITS = ("1002123456846", "348123456", "123456846", "456846", "1002-123-456846", "348-123456-01-018")


def _png(width=40, height=20, marker=(255, 0, 0)):
    from PIL import Image

    image = Image.new("RGB", (width, height), (255, 255, 255))
    image.putpixel((0, 0), marker)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _scripted_ocr(*replies):
    calls = []

    async def ocr(image):
        calls.append(image)
        reply = replies[min(len(calls), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    ocr.calls = calls
    return ocr


# --- 마스킹 --------------------------------------------------------------------------------
@pytest.mark.parametrize(("groups", "expected"), [
    (["1002", "123", "456846"], "1002-***-**6846"),
    (["348", "123456", "01", "018"], "348-******-*1-018"),
    (["110", "1234", "5678", "90"], "110-****-**78-90"),
])
def test_mask_keeps_first_group_and_last_four_digits(groups, expected):
    assert bb.mask_account(groups) == expected


def test_mask_without_separators_uses_bank_grouping_and_never_leaks_middle_digits():
    masked = bb.mask_account(bb._regroup("우리은행", "1002123456846"))
    assert masked == "1002-***-**6846"
    unknown = bb.mask_account(bb._regroup(None, "12345678901234"))
    assert unknown == "123-*******-1234"


# --- 형식 검증 · 추정 -----------------------------------------------------------------------
def test_account_format_rules_per_bank():
    assert bb.account_format_ok("우리은행", "1002123456846")
    assert not bb.account_format_ok("우리은행", "2002123456846")  # 13자리는 100 으로 시작
    assert not bb.account_format_ok("카카오뱅크", "1002123456846")
    assert bb.account_format_ok("카카오뱅크", "3333011234567")
    assert not bb.account_format_ok("IBK기업은행", "12345678")
    assert not bb.account_format_ok(None, "01012345678")  # 전화번호
    assert not bb.account_format_ok(None, "12345")


def test_phone_and_registration_numbers_are_not_account_candidates():
    text = "전화 010-1234-5678\n사업자 123-45-67890\n주민 900101-1234567\n계좌번호 1002-123-456846"
    assert [("".join(g)) for g in bb.account_candidates(text)] == ["1002123456846"]


def test_bank_is_inferred_from_format_only_when_not_printed():
    parsed = bb.parse_bankbook_text(f"예금주 김민우\n계좌번호 {RAW_IBK}")
    assert parsed["bank_name"] == "IBK기업은행" and parsed["bank_name_source"] == "inferred"
    printed = bb.parse_bankbook_text(IBK_TEXT)
    assert printed["bank_name"] == "IBK기업은행" and printed["bank_name_source"] == "printed"


def test_unknown_bank_format_is_not_guessed():
    parsed = bb.parse_bankbook_text("예금주 김민우\n계좌번호 123-456-789012")
    assert parsed["bank_name"] == "" and "bank_unknown" in parsed["reasons"]


def test_parse_reads_bank_holder_and_masked_account():
    parsed = bb.parse_bankbook_text(WOORI_TEXT)
    assert (parsed["bank_name"], parsed["bank_account_holder"], parsed["bank_account_masked"]) == (
        "우리은행", "김민우", "1002-***-**6846")
    ibk = bb.parse_bankbook_text(IBK_TEXT)
    assert (ibk["bank_account_holder"], ibk["bank_account_masked"]) == ("홍석빈", "348-******-*1-018")


def test_printed_bank_must_match_account_format():
    parsed = bb.parse_bankbook_text("카카오뱅크\n예금주 김민우\n계좌번호 1002-123-456846")
    assert parsed["bank_account_masked"] == "" and "account_format_mismatch" in parsed["reasons"]


# --- 판독 흐름: 회전 재시도 · 실패 · 신뢰도 ---------------------------------------------------
@pytest.mark.asyncio
async def test_upright_success_makes_a_single_ocr_call():
    ocr = _scripted_ocr({"text": WOORI_TEXT, "confidence": 0.9})
    result = await bb.extract_bankbook(_png(), ocr=ocr)
    assert len(ocr.calls) == 1
    assert result["extract_status"] == "extracted" and result["extract_rotation"] == 0
    assert result["bank_account_masked"] == "1002-***-**6846"


@pytest.mark.asyncio
async def test_upside_down_photo_is_retried_rotated_180():
    from PIL import Image

    source = _png(marker=(255, 0, 0))
    ocr = _scripted_ocr({"text": "", "confidence": 0.0}, {"text": IBK_TEXT, "confidence": 0.8})
    result = await bb.extract_bankbook(source, ocr=ocr)
    assert len(ocr.calls) == 2
    assert result["extract_status"] == "extracted" and result["extract_rotation"] == 180
    assert result["extract_attempts"] == 2 and result["bank_name"] == "IBK기업은행"
    second = Image.open(io.BytesIO(ocr.calls[1]))
    assert second.getpixel((second.width - 1, second.height - 1)) == (255, 0, 0)  # 180도 돌아간 이미지


@pytest.mark.asyncio
async def test_both_rotations_unreadable_is_needs_review_with_empty_values():
    ocr = _scripted_ocr({"text": "흐릿한 글자", "confidence": 0.3})
    result = await bb.extract_bankbook(_png(), ocr=ocr)
    assert len(ocr.calls) == 2
    assert result["extract_status"] == "needs_review"
    assert result["extract_reason"].startswith("account_not_found")
    assert not any(result[key] for key in ("bank_name", "bank_account_holder", "bank_account_masked", "bank_name_source"))


@pytest.mark.asyncio
async def test_ocr_exception_is_needs_review_not_an_error():
    result = await bb.extract_bankbook(_png(), ocr=_scripted_ocr(RuntimeError("tesseract 없음")))
    assert result["extract_status"] == "needs_review" and "ocr_failed" in result["extract_reason"]
    assert result["bank_account_masked"] == ""


@pytest.mark.asyncio
async def test_non_image_data_is_read_once_without_rotation():
    ocr = _scripted_ocr({"text": "", "confidence": 0})
    result = await bb.extract_bankbook(b"%PDF-1.4 not really", ocr=ocr)
    assert len(ocr.calls) == 1 and result["extract_status"] == "needs_review"


@pytest.mark.asyncio
async def test_low_confidence_blanks_values_even_when_text_parses():
    result = await bb.extract_bankbook(_png(), ocr=_scripted_ocr({"text": WOORI_TEXT, "confidence": 0.2}))
    assert result["extract_status"] == "needs_review" and "low_confidence" in result["extract_reason"]
    assert result["bank_account_masked"] == "" and result["bank_name"] == ""


@pytest.mark.asyncio
async def test_missing_holder_is_needs_review():
    result = await bb.extract_bankbook(_png(), ocr=_scripted_ocr({"text": f"우리은행\n계좌번호 {RAW_WOORI}", "confidence": 0.9}))
    assert result["extract_status"] == "needs_review" and "holder_not_found" in result["extract_reason"]


def test_exif_orientation_is_applied_before_ocr():
    from PIL import Image

    image = Image.new("RGB", (40, 20), (255, 255, 255))
    exif = Image.Exif()
    exif[0x0112] = 6  # 90도 회전 필요
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    upright, flipped = bb.prepare_images(buffer.getvalue())
    assert Image.open(io.BytesIO(upright)).size == (20, 40)
    assert Image.open(io.BytesIO(flipped)).size == (20, 40)


@pytest.mark.asyncio
async def test_result_and_logs_never_contain_raw_account_digits(caplog):
    caplog.set_level(logging.DEBUG)
    ok = await bb.extract_bankbook(_png(), ocr=_scripted_ocr({"text": WOORI_TEXT, "confidence": 0.9}))
    bad = await bb.extract_bankbook(_png(), ocr=_scripted_ocr({"text": f"계좌번호 {RAW_IBK} 흐림", "confidence": 0.3}))
    dumped = json.dumps([ok, bad], ensure_ascii=False) + caplog.text
    for raw in RAW_DIGITS:
        assert raw not in dumped
    assert "456846" not in dumped.replace("**6846", "")


def test_apply_to_record_keeps_previous_good_values_when_reextract_fails():
    record = {"id": "d1"}
    good = {**{k: "" for k in ("bank_name", "bank_name_source", "bank_account_holder", "bank_account_masked")},
            "extract_status": "extracted", "extract_reason": "", "extract_rotation": 0, "extract_attempts": 1,
            "extract_confidence": 0.9, "extracted_at": "t1", "bank_name": "우리은행", "bank_account_holder": "김민우",
            "bank_account_masked": "1002-***-**6846", "bank_name_source": "printed"}
    bb.apply_to_record(record, good)
    failed = {**good, "extract_status": "needs_review", "extract_reason": "ocr_failed", "extracted_at": "t2",
              "bank_name": "", "bank_account_holder": "", "bank_account_masked": "", "bank_name_source": ""}
    bb.apply_to_record(record, failed)
    assert record["extract_status"] == "extracted"
    assert record["extracted_fields"]["bank_account_masked"] == "1002-***-**6846"
    assert record["extract_last_failed_reason"] == "ocr_failed"


# --- 서비스 연동 -----------------------------------------------------------------------------
@pytest.fixture
def hr(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path / "uploads" / "onboarding")
    monkeypatch.setattr(service, "_db_available", lambda: False)
    service._write_file_rows("employee_join_requests", [
        {"id": "join-emp-mia", "name": "김민우", "email": EMP, "business_id": "biz-mia", "branch": "열정국밥_미아점",
         "tenant_id": TENANT, "status": "approved"},
    ])
    return tmp_path


def _upload(document_type="bankbook", data=None):
    return service.save_onboarding_document(
        employee_name="김민우", employee_email=EMP, branch="", document_type=document_type, issue_date="", memo="",
        user=OWNER, upload=UploadFile(filename="통장.png", file=BytesIO(data or _png())),
    )


@pytest.mark.asyncio
async def test_bankbook_upload_stores_masked_fields_and_contract_profile_reads_them(hr, monkeypatch):
    monkeypatch.setattr(bb, "_default_ocr", _scripted_ocr({"text": WOORI_TEXT, "confidence": 0.9}))
    document = await _upload()
    assert document["extract_status"] == "extracted"
    assert document["extracted_fields"]["bank_account_masked"] == "1002-***-**6846"
    stored = json.dumps(service._read_file_rows("onboarding_documents"), ensure_ascii=False)
    assert RAW_WOORI not in stored and "1002123456846" not in stored and "456846" not in stored.replace("**6846", "")
    profile = service._employee_onboarding_profile(
        service._read_hr("onboarding_documents", OWNER), employee_email=EMP, employee_request_id="join-emp-mia")
    assert (profile["bank_name"], profile["bank_account_holder"], profile["bank_account_masked"]) == (
        "우리은행", "김민우", "1002-***-**6846")
    assert "extract_status" not in profile


@pytest.mark.asyncio
async def test_unreadable_bankbook_upload_succeeds_with_needs_review_label(hr, monkeypatch):
    monkeypatch.setattr(bb, "_default_ocr", _scripted_ocr({"text": "", "confidence": 0}))
    document = await _upload()
    assert document["extract_status"] == "needs_review" and document["extracted_fields"] == {}
    assert document["extract_status_label"] == "계좌 확인 필요" and document["review_memo"] == "계좌 확인 필요"
    saved = service._read_file_rows("onboarding_documents")[0]
    assert saved.get("review_memo", "") == ""  # 표시용 문구는 저장하지 않는다
    profile = service._employee_onboarding_profile(
        service._read_hr("onboarding_documents", OWNER), employee_email=EMP, employee_request_id="join-emp-mia")
    assert not profile.get("bank_account_masked") and not profile.get("bank_name")


@pytest.mark.asyncio
async def test_other_document_types_are_not_run_through_ocr(hr, monkeypatch):
    ocr = _scripted_ocr({"text": WOORI_TEXT, "confidence": 0.9})
    monkeypatch.setattr(bb, "_default_ocr", ocr)
    document = await _upload(document_type="id_card")
    assert ocr.calls == [] and "extract_status" not in document


@pytest.mark.asyncio
async def test_reextract_is_admin_only_idempotent_and_bankbook_only(hr, monkeypatch):
    monkeypatch.setattr(bb, "_default_ocr", _scripted_ocr({"text": "", "confidence": 0}))
    document = await _upload()
    assert document["extract_status"] == "needs_review"

    monkeypatch.setattr(bb, "_default_ocr", _scripted_ocr({"text": IBK_TEXT, "confidence": 0.8}))
    employee = {**OWNER, "email": EMP, "current_membership": {"tenant_id": TENANT, "status": "active", "role": "member"}}
    with pytest.raises(HTTPException) as denied:
        await service.reextract_bankbook_document(document["id"], employee)
    assert denied.value.status_code == 403

    first = await service.reextract_bankbook_document(document["id"], OWNER)
    second = await service.reextract_bankbook_document(document["id"], OWNER)
    assert first["extract_status"] == second["extract_status"] == "extracted"
    assert first["extracted_fields"] == second["extracted_fields"]
    assert first["extracted_fields"]["bank_account_masked"] == "348-******-*1-018"
    rows = service._read_file_rows("onboarding_documents")
    assert len(rows) == 1 and not rows[0].get("review_memo")
    assert RAW_IBK not in json.dumps(rows, ensure_ascii=False)


@pytest.mark.asyncio
async def test_reextract_rejects_non_bankbook_and_superseded(hr, monkeypatch):
    monkeypatch.setattr(bb, "_default_ocr", _scripted_ocr({"text": "", "confidence": 0}))
    other = await _upload(document_type="id_card")
    with pytest.raises(HTTPException) as wrong_type:
        await service.reextract_bankbook_document(other["id"], OWNER)
    assert wrong_type.value.status_code == 400

    old = await _upload()
    await _upload()  # 재제출 -> 이전본은 superseded
    with pytest.raises(HTTPException) as superseded:
        await service.reextract_bankbook_document(old["id"], OWNER)
    assert superseded.value.status_code == 409


@pytest.mark.asyncio
async def test_failed_reextract_does_not_erase_previous_good_values(hr, monkeypatch):
    monkeypatch.setattr(bb, "_default_ocr", _scripted_ocr({"text": WOORI_TEXT, "confidence": 0.9}))
    document = await _upload()
    monkeypatch.setattr(bb, "_default_ocr", _scripted_ocr(RuntimeError("down")))
    result = await service.reextract_bankbook_document(document["id"], OWNER)
    assert result["extract_status"] == "extracted"
    assert result["extracted_fields"]["bank_account_masked"] == "1002-***-**6846"
