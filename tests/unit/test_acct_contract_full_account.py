"""ACCT-CONTRACT-FULL-ACCOUNT-NO-20261010 — 근로계약서 급여 계좌번호 전체 표기 + 미서명 계약 백필.

파일 모드로만 돈다(운영 DB·알리고 미접촉). 계좌번호는 전부 합성값이다(실제 직원 번호 없음).
"""
import importlib.util
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

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


# --- 7. 백필 스크립트 ----------------------------------------------------------------------------
def _contract(contract_id, status, masked=MASKED, **extra):
    return {
        "id": contract_id, "employee_request_id": "join-mia", "employee_email": EMPLOYEE["email"],
        "employee_name": "가입 직원", "status": status, "bank_account_masked": masked, "tenant_id": TENANT, **extra,
    }


def _doc(doc_id="doc-bank", status="approved", uploaded_at="2026-10-01", **extra):
    return {
        "id": doc_id, "document_type": "bankbook", "status": status, "employee_request_id": "join-mia",
        "employee_email": EMPLOYEE["email"], "uploaded_at": uploaded_at, **extra,
    }


async def _run(script, contracts, docs, number, *, apply, reads=None):
    written = []

    async def read_number(doc):
        if reads is not None:
            reads.append(doc["id"])
        return number

    rows = await script.process(contracts, docs, read_number=read_number, apply=apply, write=written.append)
    return rows, written


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing_and_reports_would_fill():
    script = _script()
    rows, written = await _run(script, [_contract("c1", "draft")], [_doc()], FULL, apply=False)
    assert written == [] and rows[0]["action"] == "would_fill" and rows[0]["last4"] == "…0111"


@pytest.mark.asyncio
async def test_backfill_apply_fills_draft_and_leaves_masked_untouched():
    script = _script()
    rows, written = await _run(script, [_contract("c1", "draft")], [_doc()], FULL, apply=True)
    assert rows[0]["action"] == "fill" and not rows[0]["revoked_request"]
    assert written[0]["bank_account_number"] == FULL and written[0]["bank_account_masked"] == MASKED
    assert written[0]["status"] == "draft"


@pytest.mark.asyncio
async def test_backfill_never_touches_signed_or_other_statuses():
    script = _script()
    reads: list[str] = []
    contracts = [_contract("s1", "signed"), _contract("x1", "cancelled"), _contract("d1", "draft", deleted_at="2026-10-02")]
    rows, written = await _run(script, contracts, [_doc()], FULL, apply=True, reads=reads)
    assert written == [] and reads == []
    assert [row["action"] for row in rows] == ["skip", "skip", "skip"]
    assert rows[0]["reason"] == "status_signed"


@pytest.mark.asyncio
async def test_backfill_skips_when_last_four_digits_differ():
    script = _script()
    rows, written = await _run(script, [_contract("c1", "draft", masked="1002-***-**9999")], [_doc()], FULL, apply=True)
    assert written == [] and rows[0]["reason"] == "mismatch"


@pytest.mark.asyncio
async def test_backfill_skips_when_digit_count_differs_unreadable_or_no_bankbook():
    script = _script()
    rows, written = await _run(script, [_contract("c1", "draft", masked="1002-***-*0111")], [_doc()], FULL, apply=True)
    assert written == [] and rows[0]["reason"] == "mismatch"
    rows, written = await _run(script, [_contract("c1", "draft")], [_doc()], "", apply=True)
    assert written == [] and rows[0]["reason"] == "unreadable"
    rows, written = await _run(script, [_contract("c1", "draft")], [_doc(status="superseded")], FULL, apply=True)
    assert written == [] and rows[0]["reason"] == "no_bankbook"


@pytest.mark.asyncio
async def test_backfill_requested_contract_returns_to_draft_with_token_revoked():
    script = _script()
    contract = _contract("r1", "requested", sign_token="tok", sign_token_hash="hash", requested_at="2026-10-09T10:00:00+09:00")
    rows, written = await _run(script, [contract], [_doc()], FULL, apply=True)
    assert rows[0]["revoked_request"] is True
    saved = written[0]
    assert saved["status"] == "draft" and saved["bank_account_number"] == FULL
    assert not any(key in saved for key in ("sign_token", "sign_token_hash", "requested_at"))
    assert contract["status"] == "requested" and contract["sign_token"] == "tok"  # 입력 레코드는 건드리지 않는다


@pytest.mark.asyncio
async def test_backfill_reads_each_bankbook_once_and_picks_latest_current_document():
    script = _script()
    reads: list[str] = []
    docs = [_doc("old", uploaded_at="2026-09-01"), _doc("new", uploaded_at="2026-10-05")]
    contracts = [_contract("c1", "draft"), _contract("c2", "draft")]
    rows, written = await _run(script, contracts, docs, FULL, apply=True, reads=reads)
    assert reads == ["new"] and len(written) == 2


@pytest.mark.asyncio
async def test_backfill_skips_contracts_that_already_have_a_number_or_no_masked():
    script = _script()
    rows, written = await _run(
        script, [_contract("c1", "draft", bank_account_number=OTHER_FULL), _contract("c2", "draft", masked="")],
        [_doc()], FULL, apply=True,
    )
    assert written == [] and [row["reason"] for row in rows] == ["already_has_number", "no_masked"]


def test_backfill_report_prints_last_four_only(capsys):
    script = _script()
    rows = [{"contract_id": "abcdef012345", "employee": "가입 직원", "status": "draft", "action": "would_fill",
             "reason": "", "last4": script.last4(FULL), "revoked_request": False}]
    script._print_report(rows, apply=False)
    out = capsys.readouterr().out
    assert "…0111" in out and FULL not in out and "999-000" not in out


def test_backfill_end_to_end_in_file_mode_changes_only_unsigned_matching_contracts(capsys):
    script = _script()
    _bankbook_doc()  # 이 변경 이전의 구 서류: masked 만 있고 전체 번호가 없다
    draft = svc.save_contract(_payload(bank_account_masked=MASKED), ADMIN)
    mismatch = svc.save_contract(_payload(bank_account_masked="1002-***-**9999"), ADMIN)
    requested = svc.request_contract_signature(svc.save_contract(_payload(bank_account_masked=MASKED), ADMIN)["id"], ADMIN)
    signed_row = {**_stored(svc.save_contract(_payload(bank_account_masked=MASKED), ADMIN)["id"]), "status": "signed"}
    svc._write_hr_record("contracts", signed_row, ADMIN)
    _bankbook_doc(bank_account_number=FULL)  # 서류에 전체 번호가 저장된 뒤

    assert script.main(["--tenant", TENANT]) == 0  # dry-run
    assert not _stored(draft["id"]).get("bank_account_number")

    assert script.main(["--tenant", TENANT, "--apply"]) == 0
    assert _stored(draft["id"])["bank_account_number"] == FULL
    assert not _stored(mismatch["id"]).get("bank_account_number")
    assert not _stored(signed_row["id"]).get("bank_account_number") and _stored(signed_row["id"])["status"] == "signed"
    after = _stored(requested["id"])
    assert after["bank_account_number"] == FULL and after["status"] == "draft" and not after.get("sign_token")
    assert FULL not in capsys.readouterr().out
