"""ACCT-EMPLOYEE-JOIN-REALNAME-REQUEST-20261006 — 직원 실명 가입과 회사/점포 입사요청, 차단 4종.

직접가입(upsert_join_request)·초대가입(accept_invite) 두 경로 모두 (1) 실명이 필수이고
(2) 회사/점포 입사요청이 대상 사업자의 고용주 테넌트에 생기며, 아래 네 가지는 막힌다.

  A. 누락 입력 — 이름 없음/이메일형 이름/회사·점포 없음
  B. 같은 사용자의 중복 요청 — 새 행을 만들지 않고, 승인 상태를 되돌리지 않는다
  C. 잘못되었거나 만료된 초대 토큰 — 저장 없음, 초대는 소진되지 않는다
  D. 다른 테넌트 회사/점포로의 요청 — 남의 테넌트에 귀속·대리 등록·초대 갈아타기 불가

오비서 DB 는 인메모리 가짜로 흉내 낸다 — 운영 DB·AADS 인증 DB 미접촉. 이메일·이름은 모두 가공값이다.
"""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app import auth as auth_module  # noqa: E402
from app.api import obys_finance as api  # noqa: E402

svc = api.svc

EMPLOYER = "15055cac-71b0-45ec-b714-7093dde189ff"
NEW_EMP_TENANT = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"
OTHER_BIZ = "biz-other-tenant"
OTHER_BRANCH = "타테넌트점"

EMP_EMAIL = "new-hire@example.com"
EMP_NAME = "김테스트"
OTHER_EMAIL = "other-hire@example.com"
INDEX_HTML = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys" / "index.html"


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
EMPLOYEE = _user(NEW_EMP_TENANT, "owner", email=EMP_EMAIL, user_id="user-new-hire")
OTHER_EMPLOYEE = _user(NEW_EMP_TENANT, "owner", email=OTHER_EMAIL, user_id="user-other-hire")
OTHER_ADMIN = _user(OTHER_TENANT, "owner", email="other-owner@example.com", user_id="user-other-owner")


class FakeObysDB:
    def __init__(self):
        self.rows: list[dict] = []
        self.mapping = {"biz-sungshin": EMPLOYER, "biz-mia": EMPLOYER, OTHER_BIZ: OTHER_TENANT}
        self.linked: list[dict] = []


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
                if row["tenant_id"] != record["tenant_id"]:
                    return False
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
        return [
            dict(r) for r in fake.rows
            if str(r.get("email") or "").lower() == email and fake.mapping.get(r.get("business_id")) == r["tenant_id"]
        ]

    async def fake_business_invite_info(business_id):
        if business_id not in fake.mapping:
            return None
        if business_id == OTHER_BIZ:
            return {"name": "타테넌트 사업자", "branches": [OTHER_BRANCH]}
        business = next((b for b in svc.CANONICAL_BUSINESSES if b["id"] == business_id), None)
        branches = [name for name, owner in svc.BUSINESS_BY_BRANCH.items() if owner == business_id]
        return {"name": business["name"] if business else business_id, "branches": branches}

    async def fake_business_ids_by_branch_name(branch):
        return [OTHER_BIZ] if branch == OTHER_BRANCH else []

    def run_db(coro):
        if coro.__name__.startswith("fake_"):
            return asyncio.run(coro)
        coro.close()
        return None

    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_run_db", run_db)
    monkeypatch.setattr(svc, "_db_fetch_ledger", fake_fetch_ledger)
    monkeypatch.setattr(svc, "_db_upsert_ledger", fake_upsert_ledger)
    monkeypatch.setattr(svc, "_db_business_tenant_id", fake_business_tenant_id)
    monkeypatch.setattr(svc, "_db_business_tenant_matches", fake_business_tenant_matches)
    monkeypatch.setattr(svc, "_db_hr_record_tenant", fake_hr_record_tenant)
    monkeypatch.setattr(svc, "_db_fetch_join_requests_by_email", fake_fetch_by_email)
    monkeypatch.setattr(svc, "_db_business_invite_info", fake_business_invite_info)
    monkeypatch.setattr(svc, "_db_business_ids_by_branch_name", fake_business_ids_by_branch_name)

    @asynccontextmanager
    async def fake_lock(tenant_id, employee_email):
        yield None

    async def fake_link(**kwargs):
        fake.linked.append(kwargs)
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


def _join(user: dict = EMPLOYEE, **extra) -> dict:
    body = {"name": EMP_NAME, "email": user["email"], "branch": "성신여대점", "business_id": "biz-sungshin", **extra}
    return svc.upsert_join_request(body, user)


def _invite(**payload) -> dict:
    body = {"name": EMP_NAME, "phone": "010-0000-0000", "targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}]}
    body.update(payload)
    return svc.create_invite(body, ADMIN)


def _stored_invite(invite_id: str) -> dict:
    return svc._find(svc._read_file_rows("employee_invites"), invite_id)


def _set_invite(invite_id: str, **fields) -> None:
    rows = svc._read_file_rows("employee_invites")
    svc._find(rows, invite_id).update(fields)
    svc._write_file_rows("employee_invites", rows)


def _status(excinfo) -> int:
    return excinfo.value.status_code


# --- 1. 정상 경로: 직접가입·초대가입 모두 실명이 담긴 입사요청이 고용주 테넌트에 생긴다 ---------------------
def test_direct_signup_creates_pending_request_with_real_name(db):
    record = _join()
    assert record["name"] == EMP_NAME
    assert record["status"] == "pending"
    assert record["business_id"] == "biz-sungshin"
    assert record["branch"] == "성신여대점"
    assert record["tenant_id"] == EMPLOYER != NEW_EMP_TENANT
    assert record["requester_user_id"] == "user-new-hire"
    assert [r["tenant_id"] for r in db.rows] == [EMPLOYER]


def test_direct_signup_resolves_business_from_branch_when_only_branch_is_given(db):
    record = svc.upsert_join_request({"name": EMP_NAME, "email": EMP_EMAIL, "branch": "성신여대점"}, EMPLOYEE)
    assert record["business_id"] == "biz-sungshin"
    assert record["tenant_id"] == EMPLOYER


def test_invite_signup_creates_a_request_per_invited_store_with_real_name(db):
    invite = _invite(targets=[
        {"business_id": "biz-sungshin", "branch": "성신여대점"},
        {"business_id": "biz-mia", "branch": "열정국밥_미아점"},
    ])
    result = svc.accept_invite({"token": invite["token"], "name": EMP_NAME}, EMPLOYEE)
    assert [r["business_id"] for r in result["requests"]] == ["biz-sungshin", "biz-mia"]
    assert {r["name"] for r in result["requests"]} == {EMP_NAME}
    assert {r["status"] for r in result["requests"]} == {"pending"}
    assert {r["tenant_id"] for r in db.rows} == {EMPLOYER}
    assert _stored_invite(invite["id"])["status"] == "accepted"


def test_invite_signup_falls_back_to_the_name_on_the_invite_not_the_email(db):
    invite = _invite(name="초대된실명")
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["request"]["name"] == "초대된실명"
    assert result["request"]["name"] != EMP_EMAIL


# --- A. 누락 입력 ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["", "   ", None])
def test_blank_real_name_is_rejected_on_direct_signup(db, name):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request({"name": name, "email": EMP_EMAIL, "business_id": "biz-sungshin", "branch": "성신여대점"}, EMPLOYEE)
    assert _status(excinfo) == 400
    assert excinfo.value.detail == svc.JOIN_NAME_REQUIRED_ERROR
    assert db.rows == []


@pytest.mark.parametrize("name", [EMP_EMAIL, "new-hire", "NEW-HIRE", "someone@example.com"])
def test_email_shaped_name_is_rejected_on_direct_signup(db, name):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request({"name": name, "email": EMP_EMAIL, "business_id": "biz-sungshin", "branch": "성신여대점"}, EMPLOYEE)
    assert _status(excinfo) == 400
    assert excinfo.value.detail == svc.JOIN_NAME_EMAIL_ERROR
    assert db.rows == []


def test_request_without_company_and_store_is_rejected(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request({"name": EMP_NAME, "email": EMP_EMAIL}, EMPLOYEE)
    assert _status(excinfo) == 400
    assert "회사(사업자)와 근무 점포" in excinfo.value.detail
    assert db.rows == []


def test_request_with_unknown_store_is_rejected(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request({"name": EMP_NAME, "email": EMP_EMAIL, "branch": "없는점포"}, EMPLOYEE)
    assert _status(excinfo) == 400
    assert db.rows == []


def test_api_schema_requires_the_name_field():
    with pytest.raises(ValidationError):
        api.JoinRequestCreate()
    with pytest.raises(ValidationError):
        api.JoinRequestCreate(email=EMP_EMAIL, business_id="biz-sungshin", branch="성신여대점")


def test_invite_accept_without_any_name_is_rejected_and_invite_is_not_burned(db):
    invite = _invite(name="")
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert _status(excinfo) == 400
    assert svc.JOIN_NAME_REQUIRED_ERROR in excinfo.value.detail
    assert db.rows == []
    assert _stored_invite(invite["id"])["status"] == "pending"


def test_invite_accept_with_email_as_name_is_rejected(db):
    invite = _invite(name="")
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": invite["token"], "name": EMP_EMAIL}, EMPLOYEE)
    assert _status(excinfo) == 400
    assert db.rows == []
    assert _stored_invite(invite["id"])["status"] == "pending"


# --- B. 같은 사용자의 중복 요청 --------------------------------------------------------------
def test_duplicate_request_from_same_user_does_not_create_a_second_row(db):
    first = _join()
    second = _join(memo="한 번 더")
    third = _join(memo="또 한 번")
    assert first["id"] == second["id"] == third["id"]
    assert len(db.rows) == 1
    assert db.rows[0]["status"] == "pending"
    assert len(svc.list_join_requests(EMPLOYEE)) == 1


def test_duplicate_request_does_not_reset_an_approved_request(db):
    first = _join()
    asyncio.run(svc.review_join_request_with_membership(first["id"], "approved", "", ADMIN))
    assert db.rows[0]["status"] == "approved"
    again = _join(memo="승인 뒤 재요청")
    assert again["id"] == first["id"]
    assert len(db.rows) == 1
    assert db.rows[0]["status"] == "approved"


def test_duplicate_request_does_not_reopen_a_rejected_request(db):
    first = _join()
    asyncio.run(svc.review_join_request_with_membership(first["id"], "rejected", "", ADMIN))
    _join(memo="반려 뒤 재요청")
    assert len(db.rows) == 1
    assert db.rows[0]["status"] == "rejected"


def test_second_company_is_a_separate_request_but_never_a_duplicate_of_the_first(db):
    first = _join()
    second = _join(business_id="biz-mia", branch="열정국밥_미아점")
    assert first["id"] != second["id"]
    assert {r["business_id"] for r in db.rows} == {"biz-sungshin", "biz-mia"}
    assert len(db.rows) == 2


def test_invite_accept_twice_by_same_user_is_idempotent(db):
    invite = _invite()
    first = svc.accept_invite({"token": invite["token"], "name": EMP_NAME}, EMPLOYEE)
    again = svc.accept_invite({"token": invite["token"], "name": EMP_NAME}, EMPLOYEE)
    assert first["request"]["id"] == again["request"]["id"]
    assert len(db.rows) == 1


def test_accepted_invite_cannot_be_reused_by_another_user(db):
    invite = _invite()
    svc.accept_invite({"token": invite["token"], "name": EMP_NAME}, EMPLOYEE)
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": invite["token"], "name": "다른사람"}, OTHER_EMPLOYEE)
    assert _status(excinfo) == 409
    assert [r["email"] for r in db.rows] == [EMP_EMAIL]


# --- C. 잘못되었거나 만료된 초대 토큰 ---------------------------------------------------------
@pytest.mark.parametrize("token", ["no-such-token", "", "   "])
def test_unknown_or_empty_invite_token_is_404_and_saves_nothing(db, token):
    _invite()
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": token, "name": EMP_NAME}, EMPLOYEE)
    assert _status(excinfo) == 404
    assert db.rows == []
    with pytest.raises(HTTPException) as resolve_exc:
        svc.resolve_invite(token)
    assert _status(resolve_exc) == 404


def test_revoked_invite_token_is_rejected(db):
    invite = _invite()
    token = invite["token"]
    svc.revoke_invite(invite["id"], ADMIN)
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": token, "name": EMP_NAME}, EMPLOYEE)
    assert _status(excinfo) == 404
    assert db.rows == []


def test_expired_invite_token_is_rejected_on_accept_and_resolve(db):
    invite = _invite()
    past = (datetime.now(svc.KST) - timedelta(minutes=1)).isoformat(timespec="seconds")
    _set_invite(invite["id"], expires_at=past)
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": invite["token"], "name": EMP_NAME}, EMPLOYEE)
    assert _status(excinfo) == 410
    assert excinfo.value.detail == "만료된 초대입니다"
    assert db.rows == []
    assert _stored_invite(invite["id"])["status"] == "pending"
    with pytest.raises(HTTPException) as resolve_exc:
        svc.resolve_invite(invite["token"])
    assert _status(resolve_exc) == 410


def test_expiry_created_by_expires_in_hours_is_enforced_end_to_end(db):
    fresh = _invite(expires_in_hours=1)
    assert svc.resolve_invite(fresh["token"])["status"] == "pending"
    stale = _invite(expires_in_hours=1)
    _set_invite(stale["id"], expires_at=(datetime.now(svc.KST) - timedelta(hours=2)).isoformat(timespec="seconds"))
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": stale["token"], "name": EMP_NAME}, EMPLOYEE)
    assert _status(excinfo) == 410
    assert svc.accept_invite({"token": fresh["token"], "name": EMP_NAME}, EMPLOYEE)["request"]["status"] == "pending"


def test_naive_expiry_timestamp_is_read_as_kst(db):
    invite = _invite()
    naive_past = (datetime.now(svc.KST) - timedelta(hours=1)).replace(tzinfo=None).isoformat(timespec="seconds")
    _set_invite(invite["id"], expires_at=naive_past)
    with pytest.raises(HTTPException) as excinfo:
        svc.resolve_invite(invite["token"])
    assert _status(excinfo) == 410


@pytest.mark.parametrize("expires_at", ["", None, "not-a-date"])
def test_legacy_invite_without_a_usable_expiry_still_works(db, expires_at):
    invite = _invite()
    _set_invite(invite["id"], expires_at=expires_at)
    assert svc.accept_invite({"token": invite["token"], "name": EMP_NAME}, EMPLOYEE)["request"]["status"] == "pending"


# --- D. 다른 테넌트 회사/점포로의 요청 -----------------------------------------------------------
def test_request_for_another_tenants_company_is_owned_by_that_company_never_by_the_requester_tenant(db):
    record = svc.upsert_join_request(
        {"name": EMP_NAME, "email": EMP_EMAIL, "business_id": OTHER_BIZ, "branch": OTHER_BRANCH}, EMPLOYEE
    )
    assert record["tenant_id"] == OTHER_TENANT
    assert record["tenant_id"] != NEW_EMP_TENANT
    # 그 회사 관리자에게만 보이고, 다른 고용주 관리자는 목록·승인 모두 못 한다.
    assert svc.list_join_requests(EMPLOYEE)[0]["id"] == record["id"]
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(svc.review_join_request_with_membership(record["id"], "approved", "", ADMIN))
    assert _status(excinfo) == 403
    assert db.rows[0]["status"] == "pending"
    assert db.linked == []


def test_employer_admin_cannot_register_a_request_into_another_tenants_company(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request(
            {"name": "대리", "email": OTHER_EMAIL, "business_id": OTHER_BIZ, "branch": OTHER_BRANCH}, ADMIN
        )
    assert _status(excinfo) == 403
    assert db.rows == []


def test_other_tenant_admin_cannot_register_into_the_employer_company(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request(
            {"name": "대리", "email": OTHER_EMAIL, "business_id": "biz-sungshin", "branch": "성신여대점"}, OTHER_ADMIN
        )
    assert _status(excinfo) == 403
    assert db.rows == []


def test_employee_cannot_file_a_request_under_someone_elses_email(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request(
            {"name": "대리", "email": OTHER_EMAIL, "business_id": "biz-sungshin", "branch": "성신여대점"}, EMPLOYEE
        )
    assert _status(excinfo) == 403
    assert db.rows == []


def test_invite_cannot_be_redirected_to_another_tenants_company_by_payload(db):
    invite = _invite()
    result = svc.accept_invite(
        {"token": invite["token"], "name": EMP_NAME, "business_id": OTHER_BIZ, "branch": OTHER_BRANCH}, EMPLOYEE
    )
    assert [r["business_id"] for r in result["requests"]] == ["biz-sungshin"]
    assert {r["tenant_id"] for r in db.rows} == {EMPLOYER}


def test_other_tenant_admin_cannot_create_an_invite_for_the_employer_company(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.create_invite(
            {"name": EMP_NAME, "targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}]}, OTHER_ADMIN
        )
    assert _status(excinfo) in (400, 403)
    assert svc._read_file_rows("employee_invites") == []


def test_unmapped_company_is_rejected_and_nothing_is_saved(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request({"name": EMP_NAME, "email": EMP_EMAIL, "business_id": "biz-unknown"}, EMPLOYEE)
    assert _status(excinfo) == 400
    assert db.rows == []


# --- 화면: 직원 첫 진입에서 실명 입력칸이 보인다 -------------------------------------------------
def _form_html(form_id: str) -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    start = html.index(f'<form id="{form_id}"')
    return html[start:html.index("</form>", start)]


@pytest.mark.parametrize("form_id", ["gateSignupForm", "gateInviteAcceptForm"])
def test_gate_forms_show_a_required_real_name_input(form_id):
    form = _form_html(form_id)
    name_inputs = re.findall(r"<input[^>]*name=\"name\"[^>]*>", form)
    assert len(name_inputs) == 1
    assert 'type="hidden"' not in name_inputs[0]
    assert 'type="text"' in name_inputs[0]
    assert "required" in name_inputs[0]
    assert "실명" in form


def test_signup_gate_asks_for_company_and_store_when_not_invited():
    form = _form_html("gateSignupForm")
    assert re.search(r"<select[^>]*name=\"businessId\"[^>]*required", form)
    assert re.search(r"<select[^>]*name=\"branch\"", form)
    assert "form.branch.required = names.length > 0" in INDEX_HTML.read_text(encoding="utf-8")


def test_client_never_sends_the_email_as_the_invite_name():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'authSession?.user?.email || "직원"' not in html
    assert "acceptPendingPhoneInvite(email)" not in html
