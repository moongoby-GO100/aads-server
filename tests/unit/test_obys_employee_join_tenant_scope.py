"""AADS-OBYS-EMPLOYEE-JOIN-TENANT-SCOPE-20261001 — 신규 테넌트 직원의 매장 가입요청과 테넌트 격리.

2026-10-01 진아244 실측: 신규 직원은 가입하면 자기 새 테넌트를 받고, /employees/* 가 레거시 게이트에서
403 이라 매장에 붙을 수 없었다.  여기서는 (1) 게이트가 직원 본인 경로 3개만 여는지, (2) 가입요청이
호출자 JWT 테넌트가 아니라 대상 사업자의 고용주 테넌트(yeoljeong_business_tenant_mapping)에 귀속되는지,
(3) 목록·승인·반려가 테넌트 경계를 넘지 못하는지 고정한다.

오비서 DB 는 인메모리 가짜(원장·사업자 매핑)로 흉내 낸다 — 운영 DB·AADS 인증 DB 미접촉.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app import auth as auth_module  # noqa: E402
from app.api import obys_finance as api  # noqa: E402
from app.core import obys_tenant  # noqa: E402

svc = api.svc

EMPLOYER = "15055cac-71b0-45ec-b714-7093dde189ff"  # 열정국밥 — 레거시 허용목록 테넌트
NEW_EMP_TENANT = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"  # 신규 직원이 가입 때 받은 개인 테넌트
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"  # 무관한 다른 사업자 테넌트
OTHER_BIZ = "biz-other-tenant"

EMP_EMAIL = "new-hire@example.com"
EMP_ID = "user-new-hire"
OTHER_EMP_EMAIL = "other-hire@example.com"


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
EMPLOYEE = _user(NEW_EMP_TENANT, "owner", email=EMP_EMAIL, user_id=EMP_ID)
OTHER_ADMIN = _user(OTHER_TENANT, "owner", email="other-owner@example.com", user_id="user-other-owner")


class FakeObysDB:
    def __init__(self):
        self.rows: list[dict] = []
        self.mapping = {"biz-sungshin": EMPLOYER, "biz-mia": EMPLOYER, OTHER_BIZ: OTHER_TENANT}


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
        return [dict(r) for r in fake.rows if str(r.get("email") or "").lower() == email and fake.mapping.get(r.get("business_id")) == r["tenant_id"]]

    def run_db(coro):
        # 가짜 코루틴만 실행한다 — 그 밖의 DB 접근(감사 INSERT 등)은 실패(None)로 취급해 파일 폴백을 탄다.
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

    fake.linked = []

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


def _apply(user: dict = EMPLOYEE, **extra) -> dict:
    return svc.upsert_join_request({"name": "신규 직원", "email": EMP_EMAIL, "branch": "성신여대점", **extra}, user)


# --- 1. 신규 테넌트 직원의 성신여대점 가입요청 -----------------------------------------------
def test_new_tenant_employee_request_is_owned_by_employer_tenant(db):
    record = _apply()
    assert record["business_id"] == "biz-sungshin"
    assert record["tenant_id"] == EMPLOYER != NEW_EMP_TENANT
    assert record["status"] == "pending"
    assert record["requester_user_id"] == EMP_ID
    assert [r["tenant_id"] for r in db.rows] == [EMPLOYER]
    # 같은 매장 재요청은 갱신이다 — 고용주 테넌트에서 기존 행을 찾는다.
    again = _apply(memo="다시")
    assert again["id"] == record["id"]
    assert len(db.rows) == 1


def test_invite_accept_uses_the_invited_business_not_payload_branch(db):
    svc._write_file_rows("employee_invites", [{
        "id": "inv-1", "token": "tok-1", "branch": "성신여대점", "name": "초대 직원", "status": "pending",
    }])
    record = svc.accept_invite({"token": "tok-1", "branch": "미아점"}, EMPLOYEE)
    assert record["business_id"] == "biz-sungshin"
    assert record["tenant_id"] == EMPLOYER
    assert record["invite_id"] == "inv-1"
    assert svc._find(svc._read_file_rows("employee_invites"), "inv-1")["status"] == "accepted"


def test_unmapped_business_is_rejected_and_nothing_is_saved(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request({"name": "직원", "email": EMP_EMAIL, "business_id": "biz-unknown"}, EMPLOYEE)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "등록되지 않은 사업자입니다"
    assert db.rows == []


def test_failed_invite_accept_does_not_burn_the_invite(db):
    svc._write_file_rows("employee_invites", [{"id": "inv-2", "token": "tok-2", "branch": "없는지점", "status": "pending"}])
    with pytest.raises(HTTPException):
        svc.accept_invite({"token": "tok-2"}, EMPLOYEE)
    assert svc._find(svc._read_file_rows("employee_invites"), "inv-2")["status"] == "pending"


def test_branch_business_mismatch_still_400(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request({"name": "직원", "email": EMP_EMAIL, "business_id": "biz-mia", "branch": "성신여대점"}, EMPLOYEE)
    assert excinfo.value.status_code == 400
    assert "일치하지 않습니다" in excinfo.value.detail


def test_cannot_file_a_request_for_someone_else_into_another_tenant(db):
    # 다른 테넌트 사용자는 남의 이메일로 고용주 테넌트에 요청을 만들 수 없다.
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request({"name": "대리", "email": OTHER_EMP_EMAIL, "branch": "성신여대점"}, EMPLOYEE)
    assert excinfo.value.status_code == 403
    assert db.rows == []
    # 다른 테넌트의 관리자도 자기 테넌트에 귀속되지 않은 사업자에는 대리 등록하지 못한다.
    with pytest.raises(HTTPException) as excinfo:
        svc.upsert_join_request({"name": "대리", "email": OTHER_EMP_EMAIL, "branch": "성신여대점"}, OTHER_ADMIN)
    assert excinfo.value.status_code == 403
    assert db.rows == []


def test_employer_admin_can_still_register_on_behalf_in_own_tenant(db):
    record = svc.upsert_join_request({"name": "대리 등록", "email": OTHER_EMP_EMAIL, "branch": "성신여대점"}, ADMIN)
    assert record["tenant_id"] == EMPLOYER
    assert record["registered_by"] == ADMIN["email"]
    assert "requester_user_id" not in record


# --- 2. 목록 -----------------------------------------------------------------------
def test_employee_lists_only_own_requests(db):
    mine = _apply()
    svc.upsert_join_request({"name": "남", "email": OTHER_EMP_EMAIL, "branch": "성신여대점"}, ADMIN)
    own = svc.list_join_requests(EMPLOYEE)
    assert [r["id"] for r in own] == [mine["id"]]
    assert all(r["email"] == EMP_EMAIL for r in own)
    # 남의 이메일 레코드만 있는 직원은 0건.
    stranger = _user(OTHER_TENANT, "owner", email="stranger@example.com", user_id="user-stranger")
    assert svc.list_join_requests(stranger) == []


def test_legacy_tenant_admin_lists_whole_tenant(db):
    mine = _apply()
    other = svc.upsert_join_request({"name": "남", "email": OTHER_EMP_EMAIL, "branch": "성신여대점"}, ADMIN)
    listed = svc.list_join_requests(ADMIN)
    assert {r["id"] for r in listed} == {mine["id"], other["id"]}


# --- 3. 승인·반려 ---------------------------------------------------------------------
def _review(request_id: str, action: str, user: dict) -> dict:
    return asyncio.run(svc.review_join_request_with_membership(request_id, action, "", user))


def test_other_tenant_admin_cannot_review(db):
    record = _apply()
    for action in ("approved", "rejected"):
        with pytest.raises(HTTPException) as excinfo:
            _review(record["id"], action, OTHER_ADMIN)
        assert excinfo.value.status_code == 403
    assert db.rows[0]["status"] == "pending"
    assert db.linked == []


def test_employee_cannot_approve_own_request(db):
    record = _apply()
    with pytest.raises(HTTPException) as excinfo:
        _review(record["id"], "approved", EMPLOYEE)
    assert excinfo.value.status_code == 403
    assert db.rows[0]["status"] == "pending"
    assert db.linked == []


def test_employer_admin_approval_links_membership(db):
    record = _apply()
    result = _review(record["id"], "approved", ADMIN)
    assert result["request"]["status"] == "approved"
    assert result["request"]["tenant_id"] == EMPLOYER
    assert result["membership"]["status"] == "linked"
    assert result["membership"]["tenant_id"] == EMPLOYER
    assert result["membership"]["role"] == "member"
    assert db.rows[0]["status"] == "approved"
    assert len(db.linked) == 1
    assert db.linked[0]["tenant_id"] == EMPLOYER
    assert db.linked[0]["employee_user_id"] == EMP_ID
    # 승인 뒤 직원 본인 목록에서도 승인 상태가 보인다.
    assert [r["status"] for r in svc.list_join_requests(EMPLOYEE)] == ["approved"]


# --- 4. 게이트: 직원 본인 경로 3개만 연다 ------------------------------------------------------
BASE = "/api/v1/yeoljeong-finance"


@pytest.mark.parametrize(
    "path",
    [f"{BASE}/employees/join-requests", f"{BASE}/employees/invites/resolve", f"{BASE}/employees/invites/accept"],
)
def test_gate_opens_employee_self_paths(path):
    assert obys_tenant._is_tenant_scoped_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        f"{BASE}/employees/invites",
        f"{BASE}/employees/approved",
        f"{BASE}/employees/approved/abc/role",
        f"{BASE}/employees/approved/abc/resync-employment",
        f"{BASE}/employees",
    ],
)
def test_gate_keeps_admin_only_employee_paths_closed(path):
    assert obys_tenant._is_tenant_scoped_path(path, "GET") is False
    assert obys_tenant._is_tenant_scoped_path(path, "POST") is False
