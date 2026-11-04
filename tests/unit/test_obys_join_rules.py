"""ACCT-OBYS-JOIN-RULES — 직원 가입 규칙 강화: 이메일 오타 차단, 휴대폰 필수, 중복 가입 차단, 계약서 중복 계정 경고.

가입·초대·검토는 인메모리 가짜 DB 로, 목록·계약 경로는 파일 모드로 돈다 — 운영 DB·알리고·AADS 인증 DB 미접촉.
이메일·이름·번호는 모두 가공값이다.
"""
from __future__ import annotations

import asyncio
import collections
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app import auth as auth_module  # noqa: E402
from app.api import obys_finance as api  # noqa: E402
from app.services import aligo_client, yeoljeong_ops_service  # noqa: E402

svc = api.svc
_REAL_SERIALIZED_WRITE = svc._serialized_employee_write

EMPLOYER = "15055cac-71b0-45ec-b714-7093dde189ff"
NEW_EMP_TENANT = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"
PHONE_A = "010-1111-2222"
PHONE_B = "010-3333-4444"


def _user(tenant_id: str, role: str, *, email: str, user_id: str) -> dict:
    return {
        "user_id": user_id,
        "email": email,
        "is_admin": False,
        "tenant_id": tenant_id,
        "tenant_role": role,
        "user_role": "user",
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": role},
    }


ADMIN = _user(EMPLOYER, "owner", email="owner@example.com", user_id="user-owner")
FIRST = _user(NEW_EMP_TENANT, "owner", email="first-hire@example.com", user_id="user-first")
SECOND = _user(NEW_EMP_TENANT, "owner", email="second-hire@example.com", user_id="user-second")


# --- 1. 이메일 공통 검증 ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "email,suggest",
    [
        ("kim@naver.comc", "naver.com"),
        ("kim@gmail.con", "gmail.com"),
        ("kim@naver.co", "naver.com"),
        ("kim@gmial.com", "gmail.com"),
        ("kim@daum.nett", "daum.net"),
        ("kim@hanmial.net", "hanmail.net"),
        ("kim@naver.comm", "naver.com"),
    ],
)
def test_main_domain_typos_are_rejected_with_a_suggestion(email, suggest):
    with pytest.raises(HTTPException) as excinfo:
        svc.validate_join_email(email)
    assert excinfo.value.status_code == 400
    assert f"혹시 {suggest} 인가요?" in excinfo.value.detail


@pytest.mark.parametrize(
    "email",
    ["kim@naver.com", "kim@gmail.com", "kim@daum.net", "kim@hanmail.net", "kim@kakao.com", "kim@nate.com",
     "kim@hotmail.com", "kim@outlook.com", "kim@icloud.com", "kim@empal.com",
     "kim@obys.invalid", "kim@example.test", "kim@aads.dev", "kim@example.com", "kim@my-company.co.kr"],
)
def test_normal_and_test_domains_pass_and_are_lowercased(email):
    assert svc.validate_join_email(email.upper()) == email


@pytest.mark.parametrize("email", ["", "no-at-sign", "kim@", "@naver.com", "kim@naver", "kim@@naver.com", "kim@naver.c1"])
def test_malformed_emails_are_rejected(email):
    with pytest.raises(HTTPException) as excinfo:
        svc.validate_join_email(email)
    assert excinfo.value.status_code == 400


def test_optional_email_may_be_blank_but_a_typo_is_still_rejected():
    assert svc.validate_join_email("", required=False) == ""
    with pytest.raises(HTTPException):
        svc.validate_join_email("kim@gmail.con", required=False)


def test_suggest_email_domain_is_silent_for_trusted_known_and_test_domains():
    for domain in ("naver.com", "gmail.com", "obys.invalid", "example.test", "aads.dev", "company.co.kr"):
        assert svc.suggest_email_domain(domain) == ""
    assert svc.suggest_email_domain("gmial.com") == "gmail.com"


# --- 2. 휴대폰 정규화·검증 ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [("010-1234-5678", "01012345678"), ("01012345678", "01012345678"), ("011-234-5678", "0112345678"),
     ("+82 10-1234-5678", "01012345678"), ("010 1234 5678", "01012345678")],
)
def test_mobile_phone_is_normalized_to_digits(raw, expected):
    assert svc.normalize_kr_mobile(raw) == expected
    assert svc.validate_join_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_blank_phone_is_rejected_when_required(raw):
    with pytest.raises(HTTPException) as excinfo:
        svc.validate_join_phone(raw)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == svc.JOIN_PHONE_REQUIRED_ERROR
    assert svc.validate_join_phone(raw, required=False) == ""


@pytest.mark.parametrize("raw", ["02-123-4567", "1234", "010-123", "010-1234-56789"])
def test_non_mobile_phone_is_rejected(raw):
    with pytest.raises(HTTPException) as excinfo:
        svc.validate_join_phone(raw, required=False)
    assert excinfo.value.status_code == 400


# --- 가입·초대·검토용 인메모리 DB ----------------------------------------------------------------
class FakeObysDB:
    def __init__(self):
        self.rows: list[dict] = []
        self.audits: list[dict] = []
        self.mapping = {"biz-sungshin": EMPLOYER, "biz-mia": EMPLOYER}


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    fake = FakeObysDB()

    async def fake_fetch_ledger(name, tenant_id=None):
        assert name == "employee_join_requests"
        return [dict(r) for r in fake.rows if tenant_id and r["tenant_id"] == tenant_id]

    async def fake_upsert_ledger(name, record):
        assert name == "employee_join_requests"
        for index, row in enumerate(fake.rows):
            if row["id"] == record["id"]:
                fake.rows[index] = dict(record)
                return True
        fake.rows.append(dict(record))
        return True

    async def fake_business_tenant_id(business_id):
        return fake.mapping.get(business_id)

    async def fake_business_tenant_matches(business_id, tenant_id):
        return fake.mapping.get(business_id) == tenant_id

    async def fake_hr_record_tenant(name, row_id):
        return next((r["tenant_id"] for r in fake.rows if r["id"] == row_id), None)

    async def fake_fetch_by_email(email):
        return [dict(r) for r in fake.rows if str(r.get("email") or "").lower() == email]

    async def fake_business_invite_info(business_id):
        if business_id not in fake.mapping:
            return None
        business = next((b for b in svc.CANONICAL_BUSINESSES if b["id"] == business_id), None)
        branches = [name for name, owner in svc.BUSINESS_BY_BRANCH.items() if owner == business_id]
        return {"name": business["name"] if business else business_id, "branches": branches}

    async def fake_business_ids_by_branch_name(branch):
        return []

    async def fake_insert_audit(**kwargs):
        fake.audits.append(kwargs)
        return {"id": f"aud-{len(fake.audits)}"}

    def run_db(coro):
        if coro.__name__.startswith("fake_"):
            return asyncio.run(coro)
        coro.close()
        return None

    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_serialized_employee_write", lambda email, operation, *lock_keys: operation())
    monkeypatch.setattr(svc, "_run_db", run_db)
    monkeypatch.setattr(svc, "_db_fetch_ledger", fake_fetch_ledger)
    monkeypatch.setattr(svc, "_db_upsert_ledger", fake_upsert_ledger)
    monkeypatch.setattr(svc, "_db_business_tenant_id", fake_business_tenant_id)
    monkeypatch.setattr(svc, "_db_business_tenant_matches", fake_business_tenant_matches)
    monkeypatch.setattr(svc, "_db_hr_record_tenant", fake_hr_record_tenant)
    monkeypatch.setattr(svc, "_db_fetch_join_requests_by_email", fake_fetch_by_email)
    monkeypatch.setattr(svc, "_db_business_invite_info", fake_business_invite_info)
    monkeypatch.setattr(svc, "_db_business_ids_by_branch_name", fake_business_ids_by_branch_name)
    monkeypatch.setattr(svc, "_db_insert_employment_audit", fake_insert_audit)

    @asynccontextmanager
    async def fake_lock(tenant_id, employee_email):
        yield None

    async def fake_link(**kwargs):
        return {
            "status": "linked",
            "tenant_id": kwargs["tenant_id"],
            "user_id": kwargs["employee_user_id"],
            "employee_email": kwargs["employee_email"],
            "owned_by_request": True,
            "before": None,
            "after": {"role": "member", "status": "active", "membership_id": "m-1"},
        }

    monkeypatch.setattr(auth_module, "employee_membership_lock", fake_lock)
    monkeypatch.setattr(auth_module, "link_employee_tenant_membership", fake_link)
    return fake


def _join(user: dict = FIRST, *, name: str = "김테스트", phone: str | None = PHONE_A, business: str = "biz-sungshin",
          branch: str = "성신여대점", **extra) -> dict:
    body = {"name": name, "email": user["email"], "branch": branch, "business_id": business, **extra}
    if phone is not None:
        body["phone"] = phone
    return svc.upsert_join_request(body, user)


def _approve(record: dict, **kwargs) -> dict:
    return asyncio.run(svc.review_join_request_with_membership(record["id"], "approved", "", ADMIN, **kwargs))


def _pending_row(db: FakeObysDB, row_id: str, email: str, phone: str, *, name: str = "김테스트", status: str = "pending") -> dict:
    row = {
        "id": row_id, "tenant_id": EMPLOYER, "business_id": "biz-sungshin", "branch": "성신여대점",
        "email": email, "name": name, "phone": phone, "status": status, "role": "employee",
        "requested_at": "2026-11-04T09:00:00+09:00",
    }
    db.rows.append(row)
    return row


# --- 3. 휴대폰 필수 -----------------------------------------------------------------------------
def test_direct_signup_without_phone_is_rejected_and_nothing_is_stored(db):
    for blank in (None, "", "   "):
        with pytest.raises(HTTPException) as excinfo:
            _join(phone=blank)
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == svc.JOIN_PHONE_REQUIRED_ERROR
    assert db.rows == []


def test_direct_signup_with_a_malformed_phone_is_rejected(db):
    with pytest.raises(HTTPException) as excinfo:
        _join(phone="02-123-4567")
    assert excinfo.value.status_code == 400
    assert db.rows == []


def test_direct_signup_rejects_an_email_domain_typo(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request(
            {"name": "김테스트", "email": "kim@naver.comc", "branch": "성신여대점", "business_id": "biz-sungshin", "phone": PHONE_A},
            _user(NEW_EMP_TENANT, "owner", email="kim@naver.comc", user_id="user-typo"),
        )
    assert excinfo.value.status_code == 400
    assert "혹시 naver.com 인가요?" in excinfo.value.detail
    assert db.rows == []


def test_signup_stores_the_phone_and_marks_it_present(db):
    record = _join(phone="+82 10-1111-2222")
    assert svc.normalize_kr_mobile(record["phone"]) == "01011112222"
    assert record["status"] == "pending"


def test_invite_creation_rejects_an_email_typo_but_does_not_require_a_phone(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.create_invite(
            {"name": "김테스트", "email": "kim@gmail.con", "targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}]}, ADMIN
        )
    assert excinfo.value.status_code == 400
    assert "혹시 gmail.com 인가요?" in excinfo.value.detail
    invite = svc.create_invite({"name": "김테스트", "targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}]}, ADMIN)
    assert invite["status"] == "pending"


def test_invite_accept_requires_a_phone(db):
    invite = svc.create_invite({"name": "김테스트", "targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}]}, ADMIN)
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": invite["token"], "name": "김테스트"}, FIRST)
    assert excinfo.value.status_code == 400
    assert svc.JOIN_PHONE_REQUIRED_ERROR in excinfo.value.detail
    assert db.rows == []
    accepted = svc.accept_invite({"token": invite["token"], "name": "김테스트", "phone": PHONE_A}, FIRST)
    assert accepted["request"]["status"] == "pending"


def test_old_blank_phone_records_are_not_retroactively_rejected(db):
    legacy = _pending_row(db, "legacy-1", FIRST["email"], "", status="approved")
    again = svc.upsert_join_request(
        {"name": "김테스트", "email": FIRST["email"], "branch": "성신여대점", "business_id": "biz-sungshin", "memo": "재요청"}, FIRST
    )
    assert again["id"] == legacy["id"]
    assert [r["status"] for r in db.rows] == ["approved"]


# --- 4. 중복 가입 차단 / 이름만 같으면 경고 -------------------------------------------------------
def test_same_business_same_phone_different_email_is_409_with_masked_account(db):
    first = _join(FIRST, phone=PHONE_A)
    with pytest.raises(HTTPException) as excinfo:
        _join(SECOND, phone="01011112222")
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == f"이미 {svc._mask_email(FIRST['email'])} 계정으로 가입돼 있습니다. 그 계정으로 로그인하세요"
    assert FIRST["email"] not in excinfo.value.detail
    assert [r["id"] for r in db.rows] == [first["id"]]


def test_duplicate_phone_is_also_blocked_against_an_approved_account(db):
    _approve(_join(FIRST, phone=PHONE_A))
    with pytest.raises(HTTPException) as excinfo:
        _join(SECOND, phone=PHONE_A)
    assert excinfo.value.status_code == 409
    assert len(db.rows) == 1


def test_duplicate_phone_blocks_invite_accept_too(db):
    _join(FIRST, phone=PHONE_A)
    invite = svc.create_invite({"name": "김테스트", "targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}]}, ADMIN)
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": invite["token"], "name": "김테스트", "phone": PHONE_A}, SECOND)
    assert excinfo.value.status_code == 409
    assert len(db.rows) == 1


def test_rejected_account_does_not_block_the_same_phone(db):
    first = _join(FIRST, phone=PHONE_A)
    asyncio.run(svc.review_join_request_with_membership(first["id"], "rejected", "", ADMIN))
    second = _join(SECOND, phone=PHONE_A)
    assert second["status"] == "pending"


def test_same_phone_in_a_different_business_is_allowed(db):
    _join(FIRST, phone=PHONE_A)
    other = _join(SECOND, phone=PHONE_A, business="biz-mia", branch="열정국밥_미아점")
    assert other["business_id"] == "biz-mia"


def test_same_name_different_phone_is_allowed_with_a_warning(db):
    _join(FIRST, name="김테스트", phone=PHONE_A)
    second = _join(SECOND, name="김테스트", phone=PHONE_B)
    assert second["status"] == "pending"
    warnings = svc.join_request_warnings(second)
    assert [w["code"] for w in warnings] == ["same_name_different_phone"]
    assert svc._mask_email(FIRST["email"]) in warnings[0]["message"]
    assert FIRST["email"] not in str(warnings)
    assert len(db.rows) == 2


def test_different_name_different_phone_has_no_warning(db):
    _join(FIRST, name="김테스트", phone=PHONE_A)
    second = _join(SECOND, name="박다른", phone=PHONE_B)
    assert svc.join_request_warnings(second) == []


def test_create_with_auto_approval_returns_the_name_warning(db, monkeypatch):
    async def disabled(tenant_id):
        return False

    monkeypatch.setattr(svc, "employee_join_auto_approve_enabled_async", disabled)
    _join(FIRST, name="김테스트", phone=PHONE_A)
    result = asyncio.run(
        svc.create_join_request_with_auto_approval(
            {"name": "김테스트", "email": SECOND["email"], "branch": "성신여대점", "business_id": "biz-sungshin", "phone": PHONE_B}, SECOND
        )
    )
    assert [w["code"] for w in result["warnings"]] == ["same_name_different_phone"]


# --- 5. 자동승인 보류 ---------------------------------------------------------------------------
def test_auto_approve_is_skipped_when_a_same_phone_account_exists(db, monkeypatch):
    async def enabled(tenant_id):
        return True

    monkeypatch.setattr(svc, "employee_join_auto_approve_enabled_async", enabled)
    _pending_row(db, "approved-1", "already@example.com", PHONE_A, status="approved")
    suspect = _pending_row(db, "suspect-1", SECOND["email"], "01011112222")
    suspect["requester_user_id"] = SECOND["user_id"]
    suspect["requester_email"] = SECOND["email"]
    outcome = asyncio.run(svc.auto_approve_join_request(suspect))
    assert outcome["approved"] is False
    assert outcome["reason"] == "duplicate_phone_suspect"
    assert next(r for r in db.rows if r["id"] == "suspect-1")["status"] == "pending"


def test_auto_approve_still_works_without_a_duplicate(db, monkeypatch):
    async def enabled(tenant_id):
        return True

    monkeypatch.setattr(svc, "employee_join_auto_approve_enabled_async", enabled)
    record = _join(FIRST, phone=PHONE_A)
    outcome = asyncio.run(svc.auto_approve_join_request(record))
    assert outcome["approved"] is True, outcome["reason"]


# --- 6. 관리자 검토: 기존 계정에 연결 / 별도 승인 + 감사 -------------------------------------------
def _suspect_setup(db: FakeObysDB) -> tuple[dict, dict]:
    existing = _pending_row(db, "existing-1", "already@example.com", PHONE_A, status="approved")
    suspect = _pending_row(db, "suspect-1", SECOND["email"], "01011112222")
    return existing, suspect


def test_approving_a_suspected_duplicate_requires_an_explicit_choice(db):
    _, suspect = _suspect_setup(db)
    with pytest.raises(HTTPException) as excinfo:
        _approve(suspect)
    assert excinfo.value.status_code == 409
    assert "link_existing" in excinfo.value.detail and "separate" in excinfo.value.detail
    assert svc._mask_email("already@example.com") in excinfo.value.detail
    assert next(r for r in db.rows if r["id"] == "suspect-1")["status"] == "pending"
    assert db.audits == []


def test_link_existing_closes_the_request_and_audits_it(db):
    existing, suspect = _suspect_setup(db)
    result = _approve(suspect, duplicate_resolution="link_existing")
    stored = next(r for r in db.rows if r["id"] == "suspect-1")
    assert stored["status"] == "rejected"
    assert result["request"]["status"] == "rejected"
    assert "기존 계정에 연결" in stored["review_memo"]
    assert stored["duplicate_resolution"]["choice"] == "link_existing"
    assert stored["linked_to_request_id"] == existing["id"]
    assert len(db.audits) == 1
    audit = db.audits[0]
    assert audit["action"] == svc.JOIN_DUPLICATE_AUDIT_ACTION
    assert audit["resource_id"] == "suspect-1"
    assert audit["details"]["choice"] == "link_existing"
    assert "already@example.com" not in str(audit)


def test_separate_approval_approves_and_audits_it(db):
    _, suspect = _suspect_setup(db)
    _approve(suspect, duplicate_resolution="separate")
    stored = next(r for r in db.rows if r["id"] == "suspect-1")
    assert stored["status"] == "approved"
    assert stored["duplicate_resolution"]["choice"] == "separate"
    assert [a["details"]["choice"] for a in db.audits] == ["separate"]


def test_unknown_resolution_value_is_treated_as_missing(db):
    _, suspect = _suspect_setup(db)
    with pytest.raises(HTTPException) as excinfo:
        _approve(suspect, duplicate_resolution="merge")
    assert excinfo.value.status_code == 409


def test_link_existing_needs_an_approved_account_to_link_to(db):
    _pending_row(db, "other-pending", "already@example.com", PHONE_A, status="pending")
    suspect = _pending_row(db, "suspect-1", SECOND["email"], PHONE_A)
    with pytest.raises(HTTPException) as excinfo:
        _approve(suspect, duplicate_resolution="link_existing")
    assert excinfo.value.status_code == 409
    assert next(r for r in db.rows if r["id"] == "suspect-1")["status"] == "pending"


def test_review_without_a_duplicate_needs_no_choice_and_writes_no_audit(db):
    record = _join(FIRST, phone=PHONE_A)
    _approve(record)
    assert next(r for r in db.rows if r["id"] == record["id"])["status"] == "approved"
    assert db.audits == []


def test_rejecting_a_suspected_duplicate_needs_no_choice(db):
    _, suspect = _suspect_setup(db)
    asyncio.run(svc.review_join_request_with_membership(suspect["id"], "rejected", "중복", ADMIN))
    assert next(r for r in db.rows if r["id"] == "suspect-1")["status"] == "rejected"


def test_join_request_list_flags_the_duplicate_suspect_with_masked_accounts(db):
    _, suspect = _suspect_setup(db)
    listed = {row["id"]: row for row in svc.list_join_requests(ADMIN)}
    assert listed["suspect-1"]["duplicate_suspect"] is True
    assert listed["suspect-1"]["duplicate_accounts"][0]["email_masked"] == svc._mask_email("already@example.com")
    assert "already@example.com" not in str(listed["suspect-1"]["duplicate_accounts"])
    assert listed["existing-1"].get("duplicate_suspect") is None


def test_join_request_list_flags_a_missing_phone(db):
    _pending_row(db, "blank-1", "blank@example.com", "")
    listed = {row["id"]: row for row in svc.list_join_requests(ADMIN)}
    assert listed["blank-1"]["phone_missing"] is True


def test_review_payload_accepts_duplicate_resolution_and_defaults_to_empty():
    assert api.ReviewPayload(action="approved").duplicate_resolution == ""
    assert api.ReviewPayload(action="approved", duplicate_resolution="separate").duplicate_resolution == "separate"


# --- 파일 모드: 승인 직원 목록·계약서 -------------------------------------------------------------
TENANT = EMPLOYER
MEMBERSHIP = {"tenant_id": TENANT, "status": "active"}
FADMIN = {"email": "owner@example.com", "is_admin": True, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
TARGET = {"email": "member@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
TWIN = {"email": "member2@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
STRANGER = {"email": "stranger@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}
SAME_NAME = {"email": "namesake@example.com", "is_admin": False, "tenant_id": TENANT, "current_membership": MEMBERSHIP}


def _frow(join_id: str, name: str, email: str, phone: str, status: str = "approved", business: str = "biz-mia") -> dict:
    return {
        "id": join_id, "name": name, "email": email, "address": "서울시 직원 주소", "phone": phone,
        "birth_date": "1990-01-01", "tenant_id": TENANT, "business_id": business, "branch": "열정국밥_미아점",
        "status": status,
    }


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture
def files(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS",
                 "OBYS_CONTRACT_PDF_FONT_PATH", "OBYS_CONTRACT_SIGN_BASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)

    async def fake_create_notification(**kwargs):
        return {"id": "ntf", **kwargs}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)


def _seed(*rows: dict) -> None:
    svc._write("employee_join_requests", list(rows))


def _contract_payload(**overrides) -> dict:
    payload = {
        "employee_request_id": "join-target", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "contract_type": "regular", "employment_tax_type": "four_insurance",
        "start_date": "2026-07-22", "contract_date": "2026-07-22", "wage_type": "monthly",
        "wage": 2800000, "workplace": "열정국밥 미아점", "job_description": "매장 운영",
        "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
        "work_days": "주 5일", "holidays": "매주 일요일", "pay_date": "매월 5일",
        "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용",
    }
    payload.update(overrides)
    return payload


def _client(user):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


# --- 7. 승인 직원 목록: duplicate_count ---------------------------------------------------------
def test_duplicate_count_includes_accounts_with_the_same_normalized_phone(files):
    _seed(
        _frow("join-target", "김가입", TARGET["email"], "010-1234-5678"),
        _frow("join-twin", "김가입", TWIN["email"], "01012345678"),
        _frow("join-solo", "박혼자", STRANGER["email"], "010-9999-0000"),
    )
    by_id = {row["id"]: row for row in svc.list_approved_employees(FADMIN)}
    assert by_id["join-target"]["duplicate_count"] == 2
    assert by_id["join-target"]["duplicate_suspect"] is True
    assert by_id["join-target"]["duplicate_basis"] == ["phone"]
    assert by_id["join-target"]["duplicate_accounts"][0]["email_masked"] == svc._mask_email(TWIN["email"])
    assert TWIN["email"] not in str(by_id["join-target"]["duplicate_accounts"])
    assert by_id["join-twin"]["duplicate_count"] == 2
    assert by_id["join-solo"]["duplicate_count"] == 1
    assert by_id["join-solo"]["duplicate_suspect"] is False


def test_duplicate_count_ignores_the_same_phone_in_another_business(files):
    _seed(
        _frow("join-target", "김가입", TARGET["email"], "010-1234-5678"),
        _frow("join-other-biz", "김가입", TWIN["email"], "010-1234-5678", business="biz-sungshin"),
    )
    by_id = {row["id"]: row for row in svc.list_approved_employees(FADMIN)}
    assert by_id["join-target"]["duplicate_count"] == 1


def test_blank_phone_rows_are_labelled_and_never_grouped_as_duplicates(files):
    _seed(
        _frow("join-a", "무번호", TARGET["email"], ""),
        _frow("join-b", "무번호둘", TWIN["email"], ""),
    )
    by_id = {row["id"]: row for row in svc.list_approved_employees(FADMIN)}
    assert by_id["join-a"]["phone_missing"] is True
    assert by_id["join-a"]["phone_missing_label"] == "휴대폰 미입력"
    assert by_id["join-a"]["duplicate_count"] == 1
    assert by_id["join-a"]["duplicate_suspect"] is False


def test_phone_present_rows_have_no_missing_label(files):
    _seed(_frow("join-a", "번호있음", TARGET["email"], "010-1234-5678"))
    row = svc.list_approved_employees(FADMIN)[0]
    assert row["phone_missing"] is False and row["phone_missing_label"] == ""


# --- 8. 계약서 발송 시 중복 계정 경고 -------------------------------------------------------------
def _dup_pair() -> None:
    _seed(
        _frow("join-target", "김가입", TARGET["email"], "010-1234-5678"),
        _frow("join-twin", "김가입", TWIN["email"], "010-1234-5678"),
        _frow("join-stranger", "박타인", STRANGER["email"], "010-7777-8888"),
    )


def test_send_warns_about_duplicate_accounts_but_still_sends(files):
    _dup_pair()
    saved = svc.save_contract(_contract_payload(), FADMIN)
    result = svc.request_contract_signature_with_notice(saved["id"], FADMIN)
    assert result["contract"]["status"] == "requested"
    warning = result["warning"]
    assert warning["code"] == "duplicate_accounts"
    assert result["warnings"] == [warning]
    emails = {item["email_masked"] for item in warning["accounts"]}
    assert emails == {svc._mask_email(TARGET["email"]), svc._mask_email(TWIN["email"])}
    assert TARGET["email"] not in str(warning) and TWIN["email"] not in str(warning)
    assert [item["is_target"] for item in warning["accounts"] if item["email_masked"] == svc._mask_email(TARGET["email"])] == [True]


def test_send_warns_for_the_same_name_with_a_different_phone(files):
    _seed(
        _frow("join-target", "김가입", TARGET["email"], "010-1234-5678"),
        _frow("join-namesake", "김가입", SAME_NAME["email"], "010-5555-6666"),
    )
    saved = svc.save_contract(_contract_payload(), FADMIN)
    result = svc.request_contract_signature_with_notice(saved["id"], FADMIN)
    assert result["contract"]["status"] == "requested"
    assert result["warning"]["code"] == "duplicate_accounts"
    assert any("name" in item["matched_by"] for item in result["warning"]["accounts"])


def test_send_has_no_warning_without_a_duplicate(files):
    _seed(
        _frow("join-target", "김가입", TARGET["email"], "010-1234-5678"),
        _frow("join-stranger", "박타인", STRANGER["email"], "010-7777-8888"),
    )
    saved = svc.save_contract(_contract_payload(), FADMIN)
    result = svc.request_contract_signature_with_notice(saved["id"], FADMIN)
    assert result["contract"]["status"] == "requested"
    assert result["warning"] is None and result["warnings"] == []


def test_pending_accounts_do_not_count_for_the_send_warning(files):
    _seed(
        _frow("join-target", "김가입", TARGET["email"], "010-1234-5678"),
        _frow("join-twin", "김가입", TWIN["email"], "010-1234-5678", status="pending"),
    )
    saved = svc.save_contract(_contract_payload(), FADMIN)
    assert svc.request_contract_signature_with_notice(saved["id"], FADMIN)["warning"] is None


def test_duplicate_check_endpoint_warns_without_changing_the_contract(files):
    _dup_pair()
    saved = svc.save_contract(_contract_payload(), FADMIN)
    response = _client(FADMIN).get(f"/yeoljeong-finance/contracts/{saved['id']}/signature-duplicate-check")
    assert response.status_code == 200
    body = response.json()
    assert body["warning"]["code"] == "duplicate_accounts" and body["warnings"] == [body["warning"]]
    stored = next(row for row in svc._read_hr("contracts", FADMIN) if row["id"] == saved["id"])
    assert stored["status"] != "requested"


def test_duplicate_check_endpoint_is_admin_only(files):
    _dup_pair()
    saved = svc.save_contract(_contract_payload(), FADMIN)
    assert _client(TARGET).get(f"/yeoljeong-finance/contracts/{saved['id']}/signature-duplicate-check").status_code == 403


# --- 9. 서명 링크를 다른 계정으로 열었을 때의 403 문구 ---------------------------------------------
def test_same_person_other_account_gets_the_relogin_message(files):
    _dup_pair()
    saved = svc.save_contract(_contract_payload(), FADMIN)
    token = svc.request_contract_signature(saved["id"], FADMIN)["sign_token"]
    with pytest.raises(HTTPException) as excinfo:
        svc.get_contract_by_token(token, TWIN)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == f"이 계약서는 {svc._mask_email(TARGET['email'])} 계정 앞으로 발송됐습니다. 그 계정으로 다시 로그인하세요"
    assert TARGET["email"] not in excinfo.value.detail


def test_unrelated_account_keeps_the_existing_403_message(files):
    _dup_pair()
    saved = svc.save_contract(_contract_payload(), FADMIN)
    token = svc.request_contract_signature(saved["id"], FADMIN)["sign_token"]
    with pytest.raises(HTTPException) as excinfo:
        svc.get_contract_by_token(token, STRANGER)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == f"이 계약서는 {svc._mask_email(TARGET['email'])} 계정으로 보냈습니다. 해당 계정으로 로그인해 주십시오"


def test_same_name_but_different_phone_account_keeps_the_existing_403_message(files):
    _seed(
        _frow("join-target", "김가입", TARGET["email"], "010-1234-5678"),
        _frow("join-namesake", "김가입", SAME_NAME["email"], "010-5555-6666"),
    )
    saved = svc.save_contract(_contract_payload(), FADMIN)
    token = svc.request_contract_signature(saved["id"], FADMIN)["sign_token"]
    with pytest.raises(HTTPException) as excinfo:
        svc.get_contract_by_token(token, SAME_NAME)
    assert excinfo.value.status_code == 403
    assert "계정으로 보냈습니다" in excinfo.value.detail


def test_same_phone_in_another_business_keeps_the_existing_403_message(files):
    _seed(
        _frow("join-target", "김가입", TARGET["email"], "010-1234-5678"),
        _frow("join-twin", "김가입", TWIN["email"], "010-1234-5678", business="biz-sungshin"),
    )
    saved = svc.save_contract(_contract_payload(), FADMIN)
    token = svc.request_contract_signature(saved["id"], FADMIN)["sign_token"]
    with pytest.raises(HTTPException) as excinfo:
        svc.get_contract_by_token(token, TWIN)
    assert "계정으로 보냈습니다" in excinfo.value.detail


def test_same_person_check_failure_falls_back_to_the_existing_message(files, monkeypatch):
    _dup_pair()
    saved = svc.save_contract(_contract_payload(), FADMIN)
    token = svc.request_contract_signature(saved["id"], FADMIN)["sign_token"]

    def boom(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(svc, "_join_phone_key", boom)
    with pytest.raises(HTTPException) as excinfo:
        svc.get_contract_by_token(token, TWIN)
    assert excinfo.value.status_code == 403
    assert "계정으로 보냈습니다" in excinfo.value.detail


def test_the_recipient_account_itself_is_not_blocked(files):
    _dup_pair()
    saved = svc.save_contract(_contract_payload(), FADMIN)
    token = svc.request_contract_signature(saved["id"], FADMIN)["sign_token"]
    assert svc.get_contract_by_token(token, TARGET)["id"] == saved["id"]


# --- 리뷰 지적 교정: 동시 가입 잠금 · 대리 등록 권한 · 계약 발송 화면 경고 ---------------------------
def _install_tenant_aware_lock(monkeypatch):
    """운영 _employee_write_lock 과 같은 규칙(키 정렬·전부 획득)의 스레드 잠금. 실제 _serialized_employee_write 를 쓴다."""
    locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
    taken: list[list[str]] = []

    @asynccontextmanager
    async def fake_lock(email, *extra_keys):
        keys = sorted({"obys-employee-write:" + email, *extra_keys})
        taken.append(keys)
        for key in keys:
            await asyncio.to_thread(locks[key].acquire)
        try:
            yield
        finally:
            for key in reversed(keys):
                locks[key].release()

    monkeypatch.setattr(svc, "_employee_write_lock", fake_lock)
    monkeypatch.setattr(svc, "_serialized_employee_write", _REAL_SERIALIZED_WRITE)
    return taken


def test_signup_lock_key_is_the_employer_tenant_not_the_email(db):
    body = {"name": "김테스트", "branch": "성신여대점", "business_id": "biz-sungshin"}
    first = svc._join_signup_lock_key(body, FIRST["email"], FIRST)
    second = svc._join_signup_lock_key(body, SECOND["email"], SECOND)
    assert first == second == "obys-join-signup:" + EMPLOYER


def test_concurrent_signups_with_different_emails_and_one_phone_store_only_one(db, monkeypatch):
    taken = _install_tenant_aware_lock(monkeypatch)
    original = svc._db_upsert_ledger

    async def fake_slow_upsert(name, record):
        await asyncio.sleep(0.3)
        return await original(name, record)

    monkeypatch.setattr(svc, "_db_upsert_ledger", fake_slow_upsert)
    outcomes: dict[str, object] = {}

    def attempt(user):
        try:
            outcomes[user["email"]] = _join(user, phone=PHONE_A)
        except HTTPException as exc:
            outcomes[user["email"]] = exc

    threads = [threading.Thread(target=attempt, args=(user,)) for user in (FIRST, SECOND)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads)
    saved = [value for value in outcomes.values() if isinstance(value, dict)]
    rejected = [value for value in outcomes.values() if isinstance(value, HTTPException)]
    assert len(saved) == 1 and len(rejected) == 1
    assert rejected[0].status_code == 409
    assert len(db.rows) == 1
    assert all("obys-join-signup:" + EMPLOYER in keys for keys in taken)


def test_a_plain_member_cannot_bypass_the_phone_rules_with_another_email(db):
    member = _user(EMPLOYER, "member", email="plain-member@example.com", user_id="user-member")
    body = {"name": "타인", "email": "someone-else@example.com", "branch": "성신여대점", "business_id": "biz-sungshin"}
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request(body, member)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == svc.JOIN_PHONE_REQUIRED_ERROR
    assert db.rows == []


def test_a_plain_member_cannot_register_a_duplicate_phone_for_another_email(db):
    _join(FIRST, phone=PHONE_A)
    member = _user(EMPLOYER, "member", email="plain-member@example.com", user_id="user-member")
    body = {"name": "타인", "email": "someone-else@example.com", "branch": "성신여대점", "business_id": "biz-sungshin", "phone": PHONE_A}
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request(body, member)
    assert excinfo.value.status_code == 409
    assert len(db.rows) == 1


def test_an_admin_may_still_register_another_employee_without_a_phone(db):
    body = {"name": "대리등록", "email": "proxy-target@example.com", "branch": "성신여대점", "business_id": "biz-sungshin"}
    record = svc.upsert_join_request(body, ADMIN)
    assert record["status"] == "pending"
    assert record["registered_by"] == ADMIN["email"]


def test_contract_editor_checks_duplicates_before_sending_and_shows_the_warning():
    source = Path(svc.__file__).resolve().parents[1].joinpath("static/apps/obys/modules/contract-editor-v41.js").read_text(encoding="utf-8")
    start = source.index("async function requestSignature()")
    body = source[start:source.index("async function resendNotice()")]
    assert "/signature-duplicate-check" in body
    assert body.index("/signature-duplicate-check") < body.index("/request-signature")
    assert "confirm(" in body
    assert "warningsOf(response)" in body
    assert "E.dupWarnings" in source and "dupWarningHtml()" in source
