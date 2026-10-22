"""AADS-OBYS-TENANT-OWNER-INVITE-GATE-20261020-R2 — 신규 테넌트 대표의 직원 초대·가입요청 승인 목록.

2026-10-20 운영 실측: 신규 테넌트 대표가 '직원 초대·가입 승인' 화면에서 GET /employees/invites 를
레거시 게이트에 막혀(403) 목록을 못 불러오고, 가입요청 목록은 본인 이메일 것만 돌아와 승인 대기가 0건이었다.
여기서는 (1) 게이트가 초대 목록·생성·취소 세 (메서드, 경로)를 열고 그 밖의 경로는 닫아 두는지(승인 직원·계약 화면 경로는 test_obys_tenant_owner_contracts_gate.py),
(2) 서비스가 비레거시 테넌트에서 호출자 테넌트의 초대·가입요청만 다루는지 고정한다.

오비서 DB 는 인메모리 가짜로 흉내 낸다 — 운영 DB·AADS 인증 DB 미접촉.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api  # noqa: E402
from app.core import obys_tenant  # noqa: E402

svc = api.svc

LEGACY = "15055cac-71b0-45ec-b714-7093dde189ff"
TENANT_A = "1bbc4f60-a1e6-462f-9a11-e3c52de4b739"
TENANT_B = "2a1b3c4d-0000-4000-8000-000000000001"
BIZ_A, BIZ_B, BIZ_LEGACY = "biz-new-a", "biz-new-b", "biz-sungshin"
BASE = "/api/v1/yeoljeong-finance"


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


OWNER_A = _user(TENANT_A, "owner", email="owner-a@example.com", user_id="u-owner-a")
MEMBER_A = _user(TENANT_A, "member", email="staff-a@example.com", user_id="u-staff-a")
OWNER_B = _user(TENANT_B, "owner", email="owner-b@example.com", user_id="u-owner-b")
LEGACY_OWNER = _user(LEGACY, "owner", email="legacy@example.com", user_id="u-legacy")

BUSINESS_NAMES = {BIZ_A: "A 사업자", BIZ_B: "B 사업자", BIZ_LEGACY: "열정국밥 성신여대점"}
MAPPING = {BIZ_A: TENANT_A, BIZ_B: TENANT_B, BIZ_LEGACY: LEGACY}


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    join_rows: list[dict] = []

    async def fake_fetch_ledger(name, tenant_id=None):
        assert name == "employee_join_requests"
        return [dict(r) for r in join_rows if tenant_id and r["tenant_id"] == tenant_id]

    async def fake_upsert_ledger(name, record):
        join_rows.append(dict(record))
        return True

    async def fake_fetch_by_email(email):
        return [dict(r) for r in join_rows if str(r.get("email") or "").lower() == email]

    async def fake_business_tenant_id(business_id):
        return MAPPING.get(business_id)

    async def fake_business_invite_info(business_id):
        if business_id not in BUSINESS_NAMES:
            return None
        return {"name": BUSINESS_NAMES[business_id], "branches": []}

    def run_db(coro):
        if coro.__name__.startswith("fake_"):
            return asyncio.run(coro)
        coro.close()
        return None

    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_run_db", run_db)
    monkeypatch.setattr(svc, "_db_fetch_ledger", fake_fetch_ledger)
    monkeypatch.setattr(svc, "_db_upsert_ledger", fake_upsert_ledger)
    monkeypatch.setattr(svc, "_db_fetch_join_requests_by_email", fake_fetch_by_email)
    monkeypatch.setattr(svc, "_db_business_tenant_id", fake_business_tenant_id)
    monkeypatch.setattr(svc, "_db_business_invite_info", fake_business_invite_info)
    return join_rows


def _join(row_id: str, tenant_id: str, email: str, **extra) -> dict:
    return {
        "id": row_id, "tenant_id": tenant_id, "email": email, "name": row_id, "status": "pending",
        "business_id": BIZ_A if tenant_id == TENANT_A else BIZ_B, "requested_at": "2026-10-20T10:00:00+09:00", **extra,
    }


def _invite(user: dict, business_id: str, **payload) -> dict:
    return svc.create_invite({"name": "직원", "targets": [{"business_id": business_id}], **payload}, user)


# --- 1. 게이트 ----------------------------------------------------------------------------------
def _request(path: str, method: str) -> SimpleNamespace:
    return SimpleNamespace(url=SimpleNamespace(path=path), method=method)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", f"{BASE}/employees/invites"),
        ("POST", f"{BASE}/employees/invites"),
        ("DELETE", f"{BASE}/employees/invites/2f6c1c0e-86d4-4f1d-9d52-0a9b8e7c1d3a"),
    ],
)
async def test_gate_opens_invite_list_create_revoke_for_new_tenant(method, path):
    user = {"tenant_id": TENANT_A}
    assert await obys_tenant.require_legacy_obys_access(_request(path, method), user) is user


@pytest.mark.parametrize(
    "method,path",
    [
        ("PATCH", f"{BASE}/employees/invites"),
        ("PUT", f"{BASE}/employees/invites"),
        ("DELETE", f"{BASE}/employees/invites"),
        ("GET", f"{BASE}/employees/invites/2f6c1c0e-86d4-4f1d-9d52-0a9b8e7c1d3a"),
        ("DELETE", f"{BASE}/employees/invites/2f6c1c0e-86d4-4f1d-9d52-0a9b8e7c1d3a/x"),
        ("GET", f"{BASE}/employees/invites-extra"),
        ("GET", f"{BASE}/employees/approved/abc/role"),
        ("PATCH", f"{BASE}/employees/approved/abc/role"),
        ("DELETE", f"{BASE}/contracts/2f6c1c0e-86d4-4f1d-9d52-0a9b8e7c1d3a"),
        ("PATCH", f"{BASE}/business-documents/2f6c1c0e-86d4-4f1d-9d52-0a9b8e7c1d3a"),
        ("GET", f"{BASE}/employees"),
    ],
)
async def test_gate_keeps_everything_else_closed_for_new_tenant(method, path):
    with pytest.raises(HTTPException) as excinfo:
        await obys_tenant.require_legacy_obys_access(_request(path, method), {"tenant_id": TENANT_A})
    assert excinfo.value.status_code == 403


# --- 2. 신규 테넌트 대표 -------------------------------------------------------------------------
def test_new_tenant_owner_creates_lists_and_revokes_own_invite(db):
    created = _invite(OWNER_A, BIZ_A)
    assert created["status"] == "pending"
    assert created["business_id"] == BIZ_A
    assert [row["id"] for row in svc.list_invites(OWNER_A)] == [created["id"]]
    revoked = svc.revoke_invite(created["id"], OWNER_A)
    assert revoked["status"] == "revoked"
    assert svc._find(svc._read_file_rows("employee_invites"), created["id"])["status"] == "revoked"


def test_new_tenant_owner_sees_only_own_invites(db):
    mine = _invite(OWNER_A, BIZ_A)
    theirs = _invite(OWNER_B, BIZ_B)
    assert [row["id"] for row in svc.list_invites(OWNER_A)] == [mine["id"]]
    assert [row["id"] for row in svc.list_invites(OWNER_B)] == [theirs["id"]]


def test_new_tenant_does_not_see_invites_with_unresolvable_owner(db):
    svc._write_file_rows("employee_invites", [
        {"id": "inv-orphan", "token": "tok-orphan", "name": "귀속불명", "branch": "알수없는지점",
         "status": "pending", "created_at": "2020-01-01T00:00:00+09:00"},
    ])
    assert svc.list_invites(OWNER_A) == []
    with pytest.raises(HTTPException) as excinfo:
        svc.revoke_invite("inv-orphan", OWNER_A)
    assert excinfo.value.status_code == 403
    # 레거시 테넌트는 종전처럼 보인다.
    assert [row["id"] for row in svc.list_invites(LEGACY_OWNER)] == ["inv-orphan"]


def test_new_tenant_owner_list_join_requests_shows_whole_own_tenant(db):
    db.extend([
        _join("jr-a1", TENANT_A, "staff-a@example.com"),
        _join("jr-a2", TENANT_A, "staff2-a@example.com", requested_at="2026-10-20T11:00:00+09:00"),
        _join("jr-a-del", TENANT_A, "gone@example.com", deleted_at="2026-10-20T12:00:00+09:00"),
        _join("jr-b1", TENANT_B, "staff-b@example.com"),
    ])
    assert [row["id"] for row in svc.list_join_requests(OWNER_A)] == ["jr-a2", "jr-a1"]
    assert [row["id"] for row in svc.list_join_requests(OWNER_B)] == ["jr-b1"]


# --- 3. 타 테넌트 대표 ---------------------------------------------------------------------------
@pytest.mark.parametrize("business_id", [BIZ_B, BIZ_LEGACY, "biz-unmapped"])
def test_owner_cannot_invite_to_business_of_another_tenant(db, business_id):
    with pytest.raises(HTTPException) as excinfo:
        _invite(OWNER_A, business_id)
    assert excinfo.value.status_code == 403
    assert svc._read_file_rows("employee_invites") == []


def test_branch_only_payload_cannot_reach_legacy_business(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.create_invite({"name": "직원", "branch": "성신여대점"}, OWNER_A)
    assert excinfo.value.status_code == 403
    with pytest.raises(HTTPException) as empty:
        svc.create_invite({"name": "직원"}, OWNER_A)
    assert empty.value.status_code == 400
    assert svc._read_file_rows("employee_invites") == []


def test_other_tenant_owner_sees_zero_invites_and_cannot_revoke(db):
    theirs = _invite(OWNER_B, BIZ_B)
    assert svc.list_invites(OWNER_A) == []
    with pytest.raises(HTTPException) as excinfo:
        svc.revoke_invite(theirs["id"], OWNER_A)
    assert excinfo.value.status_code == 403
    assert svc._find(svc._read_file_rows("employee_invites"), theirs["id"])["status"] == "pending"


def test_other_tenant_join_requests_are_invisible(db):
    db.append(_join("jr-b1", TENANT_B, "staff-b@example.com"))
    assert svc.list_join_requests(OWNER_A) == []


# --- 4. 신규 테넌트 member(직원) ------------------------------------------------------------------
def test_member_cannot_create_list_or_revoke_invites(db):
    mine = _invite(OWNER_A, BIZ_A)
    with pytest.raises(HTTPException) as created:
        _invite(MEMBER_A, BIZ_A)
    assert created.value.status_code == 403
    assert svc.list_invites(MEMBER_A) == []
    with pytest.raises(HTTPException) as revoked:
        svc.revoke_invite(mine["id"], MEMBER_A)
    assert revoked.value.status_code == 403
    assert svc._find(svc._read_file_rows("employee_invites"), mine["id"])["status"] == "pending"


def test_member_sees_only_own_join_requests(db):
    db.extend([
        _join("jr-mine", TENANT_A, "staff-a@example.com"),
        _join("jr-other", TENANT_A, "staff2-a@example.com"),
    ])
    assert [row["id"] for row in svc.list_join_requests(MEMBER_A)] == ["jr-mine"]


# --- 5. 레거시 테넌트 동작 불변 ---------------------------------------------------------------------
def test_legacy_tenant_keeps_400_for_foreign_business_and_sees_whole_tenant(db):
    with pytest.raises(HTTPException) as excinfo:
        _invite(LEGACY_OWNER, BIZ_A)
    assert excinfo.value.status_code == 400
    db.extend([_join("jr-l1", LEGACY, "a@example.com"), _join("jr-l2", LEGACY, "b@example.com")])
    assert {row["id"] for row in svc.list_join_requests(LEGACY_OWNER)} == {"jr-l1", "jr-l2"}


def test_owner_also_sees_own_request_filed_into_another_tenant(db):
    # 신규 가입자는 누구나 자기 개인 테넌트의 owner 다 — 다른 매장에 낸 본인 가입요청도 계속 보여야 한다.
    db.extend([
        _join("jr-a1", TENANT_A, "staff-a@example.com"),
        _join("jr-own-at-b", TENANT_B, OWNER_A["email"]),
        _join("jr-b-other", TENANT_B, "staff-b@example.com"),
    ])
    assert {row["id"] for row in svc.list_join_requests(OWNER_A)} == {"jr-a1", "jr-own-at-b"}
