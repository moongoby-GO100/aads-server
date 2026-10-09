"""AADS-OBYS-JOIN-BRANCH-VALIDATION-DB-20261001 — 가입요청의 사업자·지점 검증을 DB 기준으로.

DB 모드에서는 코드 상수에 없는 새 사업자·지점도 초대 생성부터 수락까지 통과하고, 가입요청은
그 사업자의 고용주 테넌트에 생긴다. 파일 모드는 종전 하드코딩 판정을 그대로 쓴다.
오비서 DB 는 인메모리 가짜로 흉내 낸다 — 운영 DB 미접촉.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api  # noqa: E402

svc = api.svc

EMPLOYER = "15055cac-71b0-45ec-b714-7093dde189ff"
NEW_EMP_TENANT = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"
EMP_EMAIL = "new-hire@example.com"

NEW_BIZ = "biz-brand-new-cafe"
NEW_BRANCH = "강남신규점"
NO_BRANCH_BIZ = "biz-no-branch-shop"
NO_BRANCH_NAME = "지점없는가게"

BUSINESS_NAMES = {
    NEW_BIZ: "신규카페",
    NO_BRANCH_BIZ: NO_BRANCH_NAME,
    "biz-sungshin": "열정국밥 성신여대점",
}
BRANCHES = {
    NEW_BIZ: [NEW_BRANCH],
    NO_BRANCH_BIZ: [],
    "biz-sungshin": ["성신여대점"],
}
MAPPING = {NEW_BIZ: EMPLOYER, NO_BRANCH_BIZ: EMPLOYER, "biz-sungshin": EMPLOYER}


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


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    rows: list[dict] = []

    async def fake_fetch_ledger(name, tenant_id=None):
        return [dict(r) for r in rows if tenant_id and r["tenant_id"] == tenant_id]

    async def fake_upsert_ledger(name, record):
        for index, row in enumerate(rows):
            if row["id"] == record["id"]:
                rows[index] = dict(record)
                return True
        rows.append(dict(record))
        return True

    async def fake_business_tenant_id(business_id):
        return MAPPING.get(business_id)

    async def fake_business_tenant_matches(business_id, tenant_id):
        return MAPPING.get(business_id) == tenant_id

    async def fake_hr_record_tenant(name, row_id):
        return next((r["tenant_id"] for r in rows if r["id"] == row_id), None)

    async def fake_fetch_by_email(email):
        return [dict(r) for r in rows if str(r.get("email") or "").lower() == email]

    async def fake_business_invite_info(business_id):
        if business_id not in BUSINESS_NAMES:
            return None
        return {"name": BUSINESS_NAMES[business_id], "branches": list(BRANCHES[business_id])}

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
    return rows


def _join(business_id: str, branch: str = "") -> dict:
    payload = {"name": "양재혁", "email": EMP_EMAIL, "business_id": business_id}
    if branch:
        payload["branch"] = branch
    return svc.upsert_join_request(payload, EMPLOYEE)


# 1. 코드 상수에 없는 새 사업자 + 그 사업자의 지점
def test_new_business_and_branch_join_request_succeeds(db):
    assert NEW_BIZ not in svc.CANONICAL_BUSINESS_IDS
    assert NEW_BRANCH not in svc.BUSINESS_BY_BRANCH
    saved = _join(NEW_BIZ, NEW_BRANCH)
    assert saved["business_id"] == NEW_BIZ
    assert saved["branch"] == NEW_BRANCH
    assert saved["tenant_id"] == EMPLOYER
    assert [r["tenant_id"] for r in db] == [EMPLOYER]


def test_accept_invite_for_new_business_lands_in_employer_tenant(db):
    invite = svc.create_invite(
        {"name": "양재혁", "phone": "010-1234-5678", "targets": [{"business_id": NEW_BIZ, "branch": NEW_BRANCH}]},
        ADMIN,
    )
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["business_id"] == NEW_BIZ
    assert result["branch"] == NEW_BRANCH
    assert result["tenant_id"] == EMPLOYER
    assert [r["tenant_id"] for r in db] == [EMPLOYER]


# 2. DB 에 없는 사업자
def test_unknown_business_is_400(db):
    with pytest.raises(HTTPException) as excinfo:
        svc._validate_join_business_branch("biz-does-not-exist", "")
    assert excinfo.value.status_code == 400
    assert "등록되지 않은 사업자" in excinfo.value.detail
    with pytest.raises(HTTPException) as excinfo:
        _join("biz-does-not-exist", NEW_BRANCH)
    assert excinfo.value.status_code == 400
    assert db == []


# 3. 그 사업자의 지점이 아닌 branch
def test_branch_of_another_business_is_400(db):
    with pytest.raises(HTTPException) as excinfo:
        _join(NEW_BIZ, "성신여대점")
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "직원의 사업자와 지점 연결이 일치하지 않습니다"
    assert db == []


# 4. 지점이 없는 사업자는 사무실형 — 빈 branch 도 예전 방식(사업자명)도 '사무실' 로 저장된다
def test_business_without_branches_and_blank_branch_succeeds(db):
    saved = _join(NO_BRANCH_BIZ)
    assert saved["business_id"] == NO_BRANCH_BIZ
    assert saved["branch"] == "사무실"
    assert saved["tenant_id"] == EMPLOYER


def test_business_without_branches_maps_its_own_name_to_office(db):
    saved = _join(NO_BRANCH_BIZ, NO_BRANCH_NAME)
    assert saved["branch"] == "사무실"


# 5. 지점이 없는 사업자 + 사업자명과 다른 branch
def test_business_without_branches_rejects_other_branch(db):
    with pytest.raises(HTTPException) as excinfo:
        _join(NO_BRANCH_BIZ, "성신여대점")
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "직원의 사업자와 지점 연결이 일치하지 않습니다"
    assert db == []


# 6. 파일 모드는 종전 하드코딩 판정
def test_file_mode_keeps_hardcoded_validation(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_db_available", lambda: False)

    def boom(_business_id):
        raise AssertionError("파일 모드는 DB 조회 헬퍼를 거치지 않는다")

    monkeypatch.setattr(svc, "_db_business_invite_info", boom)

    svc._validate_join_business_branch("biz-sungshin", "성신여대점")
    svc._validate_join_business_branch("", "")
    for business_id, branch in (
        (NEW_BIZ, NEW_BRANCH),
        ("biz-sungshin", "열정국밥_미아점"),
        ("", "성신여대점"),
    ):
        with pytest.raises(HTTPException) as excinfo:
            svc._validate_join_business_branch(business_id, branch)
        assert excinfo.value.status_code == 400
        assert excinfo.value.detail == "직원의 사업자와 지점 연결이 일치하지 않습니다"
