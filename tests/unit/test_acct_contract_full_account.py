"""ACCT-CONTRACT-FULL-ACCOUNT-NO-20261010 — 근로계약서 급여 계좌번호 전체 표기 + 미서명 계약 백필.

파일 모드로만 돈다(운영 DB·알리고 미접촉). 계좌번호는 전부 합성값이다(실제 직원 번호 없음).
"""
import importlib.util
import io
import json
import os
import time
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.services import obys_bankbook_extract as bb
from app.services import yeoljeong_contract_pdf as pdf
from app.services import yeoljeong_finance_service as svc

TENANT = "15055cac-71b0-45ec-b714-7093dde189ff"
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
ADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
EMPLOYEE = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "static" / "apps" / "obys"

FULL = "1002-999-000111"
MASKED = "1002-***-**0111"
OTHER_FULL = "1002-888-000222"


def _script():
    spec = importlib.util.spec_from_file_location("acct_backfill", ROOT / "scripts" / "acct_backfill_contract_full_account.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)
    svc._write("employee_join_requests", [{
        "id": "join-mia", "name": "가입 직원", "email": EMPLOYEE["email"], "address": "서울시 직원 주소",
        "phone": "010-1234-5678", "birth_date": "1990-01-01", "tenant_id": TENANT, "business_id": "biz-mia",
        "branch": "열정국밥_미아점", "status": "approved",
    }])


def _payload(**overrides):
    payload = {
        "employee_request_id": "join-mia", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "contract_type": "regular", "employment_tax_type": "four_insurance",
        "start_date": "2026-07-22", "contract_date": "2026-07-22", "wage_type": "monthly",
        "wage": 2800000, "workplace": "열정국밥 미아점", "job_description": "매장 운영",
        "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
        "work_days": "주 5일", "holidays": "매주 일요일", "pay_date": "매월 5일",
        "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용", "bank_name": "우리은행", "bank_account_holder": "가입 직원",
    }
    payload.update(overrides)
    return payload


def _stored(contract_id):
    return next(row for row in svc._read_hr("contracts", ADMIN) if row["id"] == contract_id)


def _bankbook_doc(**fields):
    extracted = {"bank_name": "우리은행", "bank_account_holder": "가입 직원", "bank_account_masked": MASKED}
    extracted.update(fields)
    svc._write("onboarding_documents", [{
        "id": "doc-bank", "employee_request_id": "join-mia", "employee_email": EMPLOYEE["email"],
        "employee_name": "가입 직원", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "document_type": "bankbook", "document_label": "통장사본", "status": "approved",
        "tenant_id": TENANT, "uploaded_at": "2026-10-01T09:00:00+09:00", "extracted_fields": extracted,
    }])


# --- 1. 저장: 전체 번호는 마스킹하지 않고 masked 는 파생 -------------------------------------------
def test_full_number_is_stored_unmasked_and_masked_is_derived():
    saved = svc.save_contract(_payload(bank_account_number=FULL), ADMIN)
    assert saved["bank_account_number"] == FULL
    assert saved["bank_account_masked"] == MASKED
    stored = _stored(saved["id"])
    assert stored["bank_account_number"] == FULL and stored["bank_account_masked"] == MASKED


def test_camel_case_number_is_normalised_and_overrides_a_stale_masked_value():
    saved = svc.save_contract(_payload(bankAccountNumber=FULL, bank_account_masked="1002-***-**9999"), ADMIN)
    assert saved["bank_account_number"] == FULL and "bankAccountNumber" not in saved
    assert saved["bank_account_masked"] == MASKED


def test_unseparated_number_keeps_original_form_and_derives_masked():
    saved = svc.save_contract(_payload(bank_account_number="1002999000111"), ADMIN)
    assert saved["bank_account_number"] == "1002999000111"
    assert saved["bank_account_masked"] == "100-******-0111"


@pytest.mark.parametrize("bad", ["1002-***-**0111", "abc-1234-5678", "1234", "1" * 21])
def test_invalid_number_is_rejected(bad):
    with pytest.raises(HTTPException) as exc:
        svc.save_contract(_payload(bank_account_number=bad), ADMIN)
    assert exc.value.status_code == 400 and "계좌번호" in exc.value.detail


def test_legacy_masked_only_payload_still_masks_raw_digits_and_has_no_number():
    saved = svc.save_contract(_payload(bank_account_masked="1002-999-000111"), ADMIN)
    assert saved["bank_account_masked"] == "****-***-**0111"
    assert not saved.get("bank_account_number")


def test_removing_the_number_on_edit_does_not_leave_a_stale_one():
    saved = svc.save_contract(_payload(bank_account_number=FULL), ADMIN)
    again = svc.save_contract(_payload(id=saved["id"], bank_account_masked=MASKED), ADMIN)
    assert not again.get("bank_account_number") and not _stored(saved["id"]).get("bank_account_number")


# --- 2. 직원 문서프로필 → 계약 작성 시 자동 채움 ---------------------------------------------------
def test_contract_is_auto_filled_from_the_bankbook_profile_number():
    _bankbook_doc(bank_account_number=FULL)
    saved = svc.save_contract(_payload(bank_name="", bank_account_holder=""), ADMIN)
    assert saved["bank_account_number"] == FULL and saved["bank_account_masked"] == MASKED
    assert saved["bank_name"] == "우리은행"


def test_profile_number_does_not_override_a_different_masked_account_typed_by_the_admin():
    _bankbook_doc(bank_account_number=FULL)
    saved = svc.save_contract(_payload(bank_account_masked="1002-***-**9999"), ADMIN)
    assert not saved.get("bank_account_number") and saved["bank_account_masked"] == "1002-***-**9999"


def test_explicit_number_wins_over_the_profile_number():
    _bankbook_doc(bank_account_number=FULL)
    saved = svc.save_contract(_payload(bank_account_number=OTHER_FULL), ADMIN)
    assert saved["bank_account_number"] == OTHER_FULL


def test_legacy_document_without_number_leaves_masked_fallback():
    _bankbook_doc()
    saved = svc.save_contract(_payload(bank_name="", bank_account_holder=""), ADMIN)
    assert not saved.get("bank_account_number") and saved["bank_account_masked"] == MASKED


# --- 3. 계약서 밖 화면은 마스킹 ------------------------------------------------------------------
def test_full_number_never_leaks_into_employee_list_or_document_list():
    _bankbook_doc(bank_account_number=FULL)
    employees = svc.list_approved_employees(ADMIN)
    assert employees[0]["bank_account_masked"] == MASKED
    assert "bank_account_number" not in employees[0]
    documents = svc.list_onboarding_documents(ADMIN)
    assert FULL not in str(employees) + str(documents)
    assert next(d for d in documents if d.get("id") == "doc-bank")["extracted_fields"]["bank_account_masked"] == MASKED


# --- 4. 출력: 전체 번호 우선, 없으면 masked ------------------------------------------------------
def test_pdf_term_rows_prefer_full_number_and_print_it_once():
    rows = dict(pdf.term_rows({"bank_account_number": FULL, "bank_account_masked": MASKED}))
    assert rows["계좌번호"] == FULL
    assert len([name for name, _ in pdf.term_rows({"bank_account_number": FULL, "bank_account_masked": MASKED}) if name == "계좌번호"]) == 1
    assert dict(pdf.term_rows({"bank_account_masked": MASKED}))["계좌번호"] == MASKED
    assert "bank_account_number" not in dict(pdf.extra_rows({"bank_account_number": FULL}))


def test_signed_snapshot_carries_the_full_number():
    saved = svc.save_contract(_payload(bank_account_number=FULL), ADMIN)
    snapshot, _ = svc._signed_contract_snapshot(saved)
    assert snapshot["bank_account_number"] == FULL


@pytest.mark.parametrize("relative", ["index.html", "modules/contract-core.js"])
def test_preview_prefers_full_number_then_masked(relative):
    source = (STATIC / relative).read_text(encoding="utf-8")
    assert 'contractValue(contract, "bankAccountNumber", "bank_account_number") || contractValue(contract, "bankAccountMasked", "bank_account_masked")' in source
    assert "[bankName, bankAccountDisplay, bankAccountHolder" in source


def test_form_has_full_number_input_and_no_masking_label():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "급여 계좌번호(마스킹)" not in html
    assert '<input name="bankAccountNumber"' in html and 'name="bankAccountMasked"' in html
    assert "bank_account_number: next.bankAccountNumber" in html
    assert 'els.contractForm.bankAccountNumber.value = contract.bankAccountNumber || contract.bank_account_number' in html
    editor = (STATIC / "modules" / "contract-editor-v41.js").read_text(encoding="utf-8")
    assert '"bankAccountNumber"' in editor


# --- 5. 서명요청이 나간 계약의 내용이 바뀌면 기존 서명 링크 무효화 ---------------------------------
def test_editing_a_requested_contract_revokes_the_old_link_and_allows_a_fresh_request():
    saved = svc.save_contract(_payload(bank_account_masked=MASKED), ADMIN)
    requested = svc.request_contract_signature(saved["id"], ADMIN)
    old_token = requested["sign_token"]
    assert requested["status"] == "requested"

    edited = svc.save_contract(_payload(id=saved["id"], bank_account_number=FULL), ADMIN)
    assert edited["status"] == "draft" and edited["bank_account_number"] == FULL
    assert not edited.get("sign_token") and not edited.get("sign_token_hash") and not edited.get("requested_at")
    with pytest.raises(HTTPException) as old_link:
        svc.get_contract_signing_view(old_token, EMPLOYEE)
    assert old_link.value.status_code == 403

    again = svc.request_contract_signature(saved["id"], ADMIN)
    assert again["status"] == "requested" and again["sign_token"] != old_token
    assert svc.get_contract_signing_view(again["sign_token"], EMPLOYEE)


# --- 6. 판독 모듈 헬퍼 ---------------------------------------------------------------------------
def test_parse_returns_full_number_with_separators_as_printed():
    parsed = bb.parse_bankbook_text(f"우리은행\n예금주 : 김민우\n계좌번호 {FULL}\n")
    assert parsed["bank_account_number"] == FULL and parsed["bank_account_masked"] == MASKED


@pytest.mark.parametrize(
    "number, masked, expected",
    [
        (FULL, MASKED, True),
        ("1002999000111", MASKED, True),
        (FULL, "1002-***-**9999", False),  # 끝 4자리 불일치
        (FULL, "1002-***-*0111", False),  # 자릿수 불일치
        (FULL, "2002-***-**0111", False),  # 보이는 앞자리 불일치
        (FULL, "*******0111", False) if False else (FULL, "****-***-**0111", True),
        (FULL, "1002-***-***11", False),  # 끝 4자리가 가려져 있으면 확인 불가
        (FULL, "", False),
        ("1234", "1234", False),
    ],
)
def test_account_matches_masked(number, masked, expected):
    assert bb.account_matches_masked(number, masked) is expected


# --- 7. 백필 스크립트(공용 함수 위임) ---------------------------------------------------------------
OTHER_TENANT = "99999999-aaaa-bbbb-cccc-000000000001"
OTHER_ADMIN = {
    "email": "other-owner@example.com", "is_admin": True, "tenant_id": OTHER_TENANT,
    "current_membership": {"tenant_id": OTHER_TENANT, "status": "active"},
}


def _doc(doc_id="doc-bank", status="approved", uploaded_at="2026-10-01", **extra):
    return {
        "id": doc_id, "document_type": "bankbook", "status": status, "employee_request_id": "join-mia",
        "employee_email": EMPLOYEE["email"], "uploaded_at": uploaded_at, **extra,
    }


def _save(masked=MASKED, **extra):
    return svc.save_contract(_payload(bank_account_masked=masked, **extra), ADMIN)


def _signed(masked=MASKED):
    row = {**_stored(_save(masked)["id"]), "status": "signed"}
    svc._write_hr_record("contracts", row, ADMIN)
    return row


def _all_contracts():
    return svc._read_hr("contracts", ADMIN)


async def _run(script, contracts, docs, number, *, apply, reads=None):
    async def read_number(doc):
        if reads is not None:
            reads.append(doc["id"])
        return number

    return await script.process(
        contracts, docs, read_number=read_number, apply=apply, user=svc.bankbook_autofill_actor(TENANT),
    )


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing_and_reports_would_fill():
    draft = _save()
    rows = await _run(_script(), _all_contracts(), [_doc()], FULL, apply=False)
    assert rows[0]["action"] == "would_fill" and rows[0]["last4"] == "…0111"
    assert not _stored(draft["id"]).get("bank_account_number")


@pytest.mark.asyncio
async def test_backfill_apply_fills_draft_and_leaves_masked_untouched():
    draft = _save()
    rows = await _run(_script(), _all_contracts(), [_doc()], FULL, apply=True)
    assert rows[0]["action"] == "fill" and not rows[0]["revoked_request"]
    after = _stored(draft["id"])
    assert after["bank_account_number"] == FULL and after["bank_account_masked"] == MASKED and after["status"] == "draft"


@pytest.mark.asyncio
async def test_backfill_never_touches_signed_or_other_statuses():
    signed = _signed()
    cancelled = {**_stored(_save()["id"]), "status": "cancelled"}
    svc._write_hr_record("contracts", cancelled, ADMIN)
    reads: list[str] = []
    rows = await _run(_script(), _all_contracts(), [_doc()], FULL, apply=True, reads=reads)
    assert reads == [] and {row["action"] for row in rows} == {"skip"}
    assert {row["reason"] for row in rows} == {"status_signed", "status_cancelled"}
    assert not _stored(signed["id"]).get("bank_account_number") and _stored(signed["id"])["status"] == "signed"


@pytest.mark.asyncio
async def test_backfill_skips_when_masked_and_number_mismatch():
    for masked in ("1002-***-**9999", "1002-***-*0111"):
        draft = _save(masked)
        rows = await _run(_script(), [_stored(draft["id"])], [_doc()], FULL, apply=True)
        assert rows[0]["reason"] == "mismatch"
        assert not _stored(draft["id"]).get("bank_account_number")


@pytest.mark.asyncio
async def test_backfill_skips_unreadable_or_no_bankbook():
    draft = _save()
    contract = _stored(draft["id"])
    rows = await _run(_script(), [contract], [_doc()], "", apply=True)
    assert rows[0]["reason"] == "unreadable"
    rows = await _run(_script(), [contract], [_doc(status="superseded")], FULL, apply=True)
    assert rows[0]["reason"] == "no_bankbook"
    assert not _stored(draft["id"]).get("bank_account_number")


@pytest.mark.asyncio
async def test_backfill_requested_contract_is_reissued_with_a_new_token():
    requested = svc.request_contract_signature(_save()["id"], ADMIN)
    old_token = requested["sign_token"]
    rows = await _run(_script(), [_stored(requested["id"])], [_doc()], FULL, apply=True)
    assert rows[0]["revoked_request"] is True and rows[0]["reissued"] is True
    after = _stored(requested["id"])
    assert after["bank_account_number"] == FULL and after["status"] == "requested"
    assert after["sign_token"] and after["sign_token"] != old_token


@pytest.mark.asyncio
async def test_backfill_reads_each_bankbook_once_and_picks_latest_current_document():
    _save()
    _save()
    reads: list[str] = []
    docs = [_doc("old", uploaded_at="2026-09-01"), _doc("new", uploaded_at="2026-10-05")]
    rows = await _run(_script(), _all_contracts(), docs, FULL, apply=True, reads=reads)
    assert reads == ["new"] and [row["action"] for row in rows] == ["fill", "fill"]


@pytest.mark.asyncio
async def test_backfill_skips_contracts_that_already_have_a_number_or_no_masked():
    has_number = _save(bank_account_number=OTHER_FULL)
    no_masked = svc.save_contract(_payload(bank_name="", bank_account_holder=""), ADMIN)
    rows = await _run(_script(), [_stored(has_number["id"]), _stored(no_masked["id"])], [_doc()], FULL, apply=True)
    assert [row["reason"] for row in rows] == ["already_has_number", "no_masked"]


def test_backfill_has_no_duplicated_apply_logic():
    source = (ROOT / "scripts" / "acct_backfill_contract_full_account.py").read_text(encoding="utf-8")
    assert "autofill_contract_bank_account" in source and "contract_account_autofill_skip_reason" in source
    assert "account_matches_masked" not in source and "_write_hr_record" not in source and "_revoke_contract_signature_request" not in source


def test_backfill_report_prints_last_four_only(capsys):
    rows = [{"contract_id": "abcdef012345", "employee": "가입 직원", "status": "draft", "action": "would_fill",
             "reason": "", "last4": svc.account_last4(FULL), "revoked_request": False, "reissued": False}]
    _script()._print_report(rows, apply=False)
    out = capsys.readouterr().out
    assert "…0111" in out and FULL not in out and "999-000" not in out


def test_backfill_end_to_end_in_file_mode_changes_only_unsigned_matching_contracts(capsys):
    script = _script()
    _bankbook_doc()  # 이 변경 이전의 구 서류: masked 만 있고 전체 번호가 없다
    draft = _save()
    mismatch = _save("1002-***-**9999")
    requested = svc.request_contract_signature(_save()["id"], ADMIN)
    signed_row = _signed()
    _bankbook_doc(bank_account_number=FULL)  # 서류에 전체 번호가 저장된 뒤

    assert script.main(["--tenant", TENANT]) == 0  # dry-run
    assert not _stored(draft["id"]).get("bank_account_number")

    assert script.main(["--tenant", TENANT, "--apply"]) == 0
    assert _stored(draft["id"])["bank_account_number"] == FULL
    assert not _stored(mismatch["id"]).get("bank_account_number")
    assert not _stored(signed_row["id"]).get("bank_account_number") and _stored(signed_row["id"])["status"] == "signed"
    after = _stored(requested["id"])
    assert after["bank_account_number"] == FULL and after["status"] == "requested"
    assert after["sign_token"] and after["sign_token"] != requested["sign_token"]
    assert FULL not in capsys.readouterr().out


# --- 8. 업로드·재판독 시 자동 반영 ---------------------------------------------------------------------
def _autofill(number=FULL, **kwargs):
    return svc.autofill_employee_contract_accounts(
        number, employee_request_id="join-mia", employee_email=EMPLOYEE["email"],
        user=svc.bankbook_autofill_actor(TENANT), source="test", **kwargs,
    )


def _audit_rows():
    return [row for row in svc._read_file_rows("employment_audit_logs") if row.get("action") == svc.BANKBOOK_AUTOFILL_ACTION]


def test_autofill_fills_draft_when_masked_matches_and_writes_a_last4_only_audit():
    draft = _save()
    rows = _autofill()
    assert [row["action"] for row in rows] == ["fill"]
    assert _stored(draft["id"])["bank_account_number"] == FULL
    audit = _audit_rows()
    assert len(audit) == 1 and audit[0]["actor"] == svc.BANKBOOK_AUTOFILL_ACTOR and audit[0]["resource_id"] == draft["id"]
    dumped = json.dumps(audit, ensure_ascii=False)
    assert "…0111" in dumped and FULL not in dumped and "999-000" not in dumped and "1002999000111" not in dumped


def test_autofill_does_not_apply_on_mismatch_and_leaves_the_contract_alone():
    draft = _save("1002-***-**9999")
    rows = _autofill()
    assert [(row["action"], row["reason"]) for row in rows] == [("skip", "mismatch")]
    assert not _stored(draft["id"]).get("bank_account_number") and _audit_rows() == []


def test_autofill_never_touches_signed_contracts():
    signed = _signed()
    before = _stored(signed["id"])
    rows = _autofill()
    assert [(row["action"], row["reason"]) for row in rows] == [("skip", "status_signed")]
    assert _stored(signed["id"]) == before and _audit_rows() == []


def test_autofill_reissues_requested_contract_with_a_new_token_without_admin_click():
    requested = svc.request_contract_signature(_save()["id"], ADMIN)
    old_token = requested["sign_token"]
    rows = _autofill()
    assert rows[0]["action"] == "fill" and rows[0]["revoked_request"] and rows[0]["reissued"]
    after = _stored(requested["id"])
    assert after["status"] == "requested" and after["bank_account_number"] == FULL
    assert after["sign_token"] and after["sign_token"] != old_token
    with pytest.raises(HTTPException) as old_link:
        svc.get_contract_signing_view(old_token, EMPLOYEE)
    assert old_link.value.status_code == 403
    assert svc.get_contract_signing_view(after["sign_token"], EMPLOYEE)
    assert _audit_rows()[0]["details"]["signature_reissued"] is True


def test_autofill_keeps_the_number_when_reissue_fails(monkeypatch):
    requested = svc.request_contract_signature(_save()["id"], ADMIN)

    def boom(*args, **kwargs):
        raise RuntimeError("notify down")

    monkeypatch.setattr(svc, "request_contract_signature_with_notice", boom)
    rows = _autofill()
    assert rows[0]["action"] == "fill" and rows[0]["reissued"] is False and rows[0]["reissue_error"] == "RuntimeError"
    after = _stored(requested["id"])
    assert after["bank_account_number"] == FULL and after["status"] == "draft" and not after.get("sign_token")


def test_autofill_skips_other_employees_contracts():
    mine = _save()
    svc._write("employee_join_requests", [
        *svc._read_file_rows("employee_join_requests"),
        {"id": "join-other", "name": "다른 직원", "email": "other@example.com", "tenant_id": TENANT,
         "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "approved", "address": "서울", "phone": "010-0000-0000",
         "birth_date": "1991-01-01"},
    ])
    other = svc.save_contract(_payload(employee_request_id="join-other", bank_account_masked=MASKED), ADMIN)
    _autofill()
    assert _stored(mine["id"])["bank_account_number"] == FULL
    assert not _stored(other["id"]).get("bank_account_number")


def test_autofill_is_tenant_isolated():
    draft = _save()
    contract = _stored(draft["id"])
    # 다른 테넌트 주체는 이 계약을 읽지도 쓰지도 못한다.
    assert svc.autofill_employee_contract_accounts(
        FULL, employee_request_id="join-mia", employee_email=EMPLOYEE["email"],
        user=svc.bankbook_autofill_actor(OTHER_TENANT), source="test",
    ) == []
    # 계약 레코드를 직접 넘겨도 테넌트가 다르면 건너뛰고 쓰지 않는다.
    rows = svc.autofill_employee_contract_accounts(
        FULL, employee_request_id="join-mia", employee_email=EMPLOYEE["email"],
        user=svc.bankbook_autofill_actor(OTHER_TENANT), source="test", contracts=[contract],
    )
    assert [(row["action"], row["reason"]) for row in rows] == [("skip", "tenant_mismatch")]
    assert not _stored(draft["id"]).get("bank_account_number") and _audit_rows() == []
    with pytest.raises(HTTPException):
        svc.autofill_contract_bank_account(contract, FULL, {"email": "x@y.z", "tenant_id": "", "is_admin": True}, source="test")


def test_autofill_is_idempotent():
    _save()
    assert [row["action"] for row in _autofill()] == ["fill"]
    assert [row["reason"] for row in _autofill()] == ["already_has_number"]
    assert len(_audit_rows()) == 1


def _scripted_ocr(reply):
    async def ocr(image):
        return reply

    return ocr


def _png_bytes():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), (255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _upload_bankbook(user=ADMIN):
    return svc.save_onboarding_document(
        employee_name="가입 직원", employee_email=EMPLOYEE["email"], branch="", document_type="bankbook", issue_date="",
        memo="", user=user, upload=UploadFile(filename="통장.png", file=io.BytesIO(_png_bytes())),
    )


@pytest.mark.asyncio
async def test_bankbook_upload_auto_fills_matching_draft_and_requested_contracts(monkeypatch, caplog):
    monkeypatch.setattr(svc, "_db_available", lambda: False)
    monkeypatch.setattr(bb, "_default_ocr", _scripted_ocr({"text": f"우리은행\n예금주 : 가입 직원\n계좌번호 {FULL}\n", "confidence": 0.9}))
    draft = _save()
    requested = svc.request_contract_signature(_save()["id"], ADMIN)
    signed = _signed()
    mismatch = _save("1002-***-**9999")
    caplog.set_level("DEBUG")
    document = await _upload_bankbook()
    assert document["extract_status"] == "extracted"
    assert _stored(draft["id"])["bank_account_number"] == FULL
    assert _stored(requested["id"])["sign_token"] != requested["sign_token"]
    assert _stored(requested["id"])["bank_account_number"] == FULL and _stored(requested["id"])["status"] == "requested"
    assert not _stored(signed["id"]).get("bank_account_number")
    assert not _stored(mismatch["id"]).get("bank_account_number")
    assert FULL not in caplog.text and FULL not in json.dumps(document, ensure_ascii=False)


@pytest.mark.asyncio
async def test_bankbook_upload_with_unreadable_scan_changes_no_contract(monkeypatch):
    monkeypatch.setattr(svc, "_db_available", lambda: False)
    monkeypatch.setattr(bb, "_default_ocr", _scripted_ocr({"text": "", "confidence": 0}))
    draft = _save()
    document = await _upload_bankbook()
    assert document["extract_status"] != "extracted"
    assert not _stored(draft["id"]).get("bank_account_number")


@pytest.mark.asyncio
async def test_autofill_failure_never_breaks_the_upload(monkeypatch):
    monkeypatch.setattr(svc, "_db_available", lambda: False)
    monkeypatch.setattr(bb, "_default_ocr", _scripted_ocr({"text": f"우리은행\n예금주 : 가입 직원\n계좌번호 {FULL}\n", "confidence": 0.9}))

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(svc, "autofill_employee_contract_accounts", boom)
    _save()
    document = await _upload_bankbook()
    assert document["extract_status"] == "extracted"


@pytest.mark.asyncio
async def test_autofill_has_a_bounded_timeout(monkeypatch):
    monkeypatch.setenv("OBYS_BANKBOOK_AUTOFILL_TIMEOUT_SEC", "1")

    def slow(*args, **kwargs):
        time.sleep(3)
        return []

    monkeypatch.setattr(svc, "autofill_employee_contract_accounts", slow)
    record = {"id": "d1", "extract_status": bb.STATUS_EXTRACTED, "employee_request_id": "join-mia",
              "employee_email": EMPLOYEE["email"], "extracted_fields": {"bank_account_number": FULL}}
    started = time.monotonic()
    assert await svc._autofill_contracts_from_bankbook(record, TENANT, source="test") == []
    assert time.monotonic() - started < 2.5


@pytest.mark.asyncio
async def test_autofill_ignores_non_extracted_or_superseded_documents(monkeypatch):
    calls = []
    monkeypatch.setattr(svc, "autofill_employee_contract_accounts", lambda *a, **k: calls.append(1) or [])
    base = {"id": "d1", "employee_request_id": "join-mia", "employee_email": EMPLOYEE["email"],
            "extracted_fields": {"bank_account_number": FULL}}
    for record in (
        {**base, "extract_status": bb.STATUS_NEEDS_REVIEW},
        {**base, "extract_status": bb.STATUS_EXTRACTED, "status": "superseded"},
        {**base, "extract_status": bb.STATUS_EXTRACTED, "extracted_fields": {}},
    ):
        assert await svc._autofill_contracts_from_bankbook(record, TENANT, source="test") == []
    assert calls == []


# --- 9. 화면: 가려진 번호와 읽은 번호가 다르면 자동 반영 없이 경고만 ------------------------------------------
def test_contract_ui_warns_account_check_needed_on_mismatch_and_reuses_the_bank_warning_modal():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "계좌 확인 필요" in html and "bankbookAccountMismatch" in html and "maskedAccountsAgree" in html
    assert 'id="contractBankWarnText"' in html
    assert html.count('id="contractBankWarnModal"') == 1  # 새 모달을 만들지 않고 기존 경고를 재사용
    assert '["bankbook", "bankbook_copy"].includes(item.document_type)' in html
    assert 'bank_account_masked' in html and "openContractBankWarn(mismatched, true)" in html
