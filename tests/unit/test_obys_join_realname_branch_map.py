"""AADS-OBYS-JOIN-REALNAME-AND-BRANCH-MAP-20261001 — 가입 실명 필수화 + 지점→사업자 DB 해석.

가입요청이 business_id 를 직접 받고, 지정이 없으면 상수에 없는 지점도 DB 로 사업자를 찾는다.
같은 이름 지점이 두 사업자에 있으면 사업자를 지정하라고 400 을 낸다. 오비서 DB 는 인메모리 가짜.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

import app.auth as auth_module  # noqa: E402
from app.api import auth as auth_router  # noqa: E402
from app.api import obys_finance as api  # noqa: E402

svc = api.svc

EMPLOYER = "15055cac-71b0-45ec-b714-7093dde189ff"
OTHER_EMPLOYER = "6b0c7b02-0d6f-4a5e-9b7e-2f3f3d9a1c11"
NEW_EMP_TENANT = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"
EMP_EMAIL = "new-hire@example.com"

NEW_BIZ = "biz-brand-new-cafe"
NEW_BRANCH = "강남신규점"
DUP_BIZ = "biz-second-cafe"

BUSINESS_NAMES = {NEW_BIZ: "신규카페", DUP_BIZ: "두번째카페", "biz-sungshin": "열정국밥 성신여대점"}
BRANCHES = {NEW_BIZ: [NEW_BRANCH], DUP_BIZ: [], "biz-sungshin": ["성신여대점"]}
MAPPING = {NEW_BIZ: EMPLOYER, DUP_BIZ: OTHER_EMPLOYER, "biz-sungshin": EMPLOYER}


def _user(tenant_id: str, *, email: str, user_id: str) -> dict:
    return {
        "user_id": user_id,
        "email": email,
        "is_admin": False,
        "tenant_id": tenant_id,
        "tenant_role": "owner",
        "user_role": "user",
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": "owner"},
    }


EMPLOYEE = _user(NEW_EMP_TENANT, email=EMP_EMAIL, user_id="user-new-hire")


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    rows: list[dict] = []
    branch_owners = {name: [biz] for biz, names in BRANCHES.items() for name in names}

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

    async def fake_business_ids_by_branch_name(branch):
        return list(branch_owners.get(branch, []))

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
    return rows, branch_owners


def _join(**extra) -> dict:
    payload = api.JoinRequestCreate(name="양재혁", email=EMP_EMAIL, **extra).model_dump()
    return svc.upsert_join_request(payload, EMPLOYEE)


def test_explicit_business_id_is_accepted_and_attributed(db):
    db, _owners = db
    saved = _join(business_id="biz-sungshin", branch="성신여대점")
    assert saved["business_id"] == "biz-sungshin"
    assert saved["tenant_id"] == EMPLOYER
    assert [r["tenant_id"] for r in db] == [EMPLOYER]


def test_join_request_model_defaults_business_id_blank():
    assert api.JoinRequestCreate(name="양재혁").business_id == ""


def test_constant_branch_without_business_id_still_works(db):
    db, _owners = db
    saved = _join(branch="성신여대점")
    assert saved["business_id"] == "biz-sungshin"


def test_db_only_branch_resolves_business_without_business_id(db):
    db, _owners = db
    assert NEW_BRANCH not in svc.BUSINESS_BY_BRANCH
    saved = _join(branch=NEW_BRANCH)
    assert saved["business_id"] == NEW_BIZ
    assert saved["branch"] == NEW_BRANCH
    assert saved["tenant_id"] == EMPLOYER


def test_same_branch_name_in_two_businesses_requires_business_id(db):
    db, owners = db
    owners[NEW_BRANCH] = [NEW_BIZ, DUP_BIZ]
    with pytest.raises(HTTPException) as excinfo:
        _join(branch=NEW_BRANCH)
    assert excinfo.value.status_code == 400
    assert "사업자를 지정" in excinfo.value.detail
    assert db == []


def test_ambiguous_branch_is_resolved_by_explicit_business_id(db):
    db, owners = db
    owners[NEW_BRANCH] = [NEW_BIZ, DUP_BIZ]
    saved = _join(business_id=NEW_BIZ, branch=NEW_BRANCH)
    assert saved["business_id"] == NEW_BIZ
    assert saved["tenant_id"] == EMPLOYER


def test_unknown_branch_without_business_id_stays_400(db):
    db, _owners = db
    with pytest.raises(HTTPException) as excinfo:
        _join(branch="어디에도없는점")
    assert excinfo.value.status_code == 400
    assert db == []


def test_file_mode_never_touches_db_resolution(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_db_available", lambda: False)

    def boom(_branch):
        raise AssertionError("파일 모드는 DB 지점 조회를 거치지 않는다")

    monkeypatch.setattr(svc, "_db_business_ids_by_branch_name", boom)
    assert svc._resolve_join_business_by_branch(NEW_BRANCH) == ""
    saved = _join(branch="성신여대점")
    assert saved["business_id"] == "biz-sungshin"


# --- /auth/register 실명 필수 ---


def _register_client(monkeypatch):
    seen: dict = {}

    async def fake_ready():
        return None

    async def fake_by_email(email):
        return None

    async def fake_create_user(email, password, name, **kwargs):
        seen["name"] = name
        return {"id": "u1", "email": email, "name": name, "default_tenant_id": None, "created_at": None}

    async def fake_create_tenant(*, user_id, name, plan_key):
        return {"tenant_id": "t1", "name": name}

    monkeypatch.setattr(auth_module, "require_saas_schema_ready", fake_ready)
    monkeypatch.setattr(auth_module, "get_saas_user_by_email", fake_by_email)
    monkeypatch.setattr(auth_module, "create_saas_user", fake_create_user)
    monkeypatch.setattr(auth_module, "create_tenant_for_user", fake_create_tenant)
    monkeypatch.setattr(auth_module, "create_token", lambda *a, **kw: "tok")
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1")
    return TestClient(app), seen


@pytest.mark.parametrize(
    "name",
    [None, "", "   ", "covette84", "COVETTE84", " covette84 "],
)
def test_register_rejects_blank_or_email_localpart_name(monkeypatch, name):
    client, seen = _register_client(monkeypatch)
    body = {"email": "covette84@example.com", "password": "abcdef"}
    if name is not None:
        body["name"] = name
    resp = client.post("/api/v1/auth/register", json=body)
    assert resp.status_code == 400, resp.text
    assert "name" not in seen


def test_register_phone_localpart_name_is_rejected(monkeypatch):
    client, seen = _register_client(monkeypatch)
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "01191535581@example.com", "password": "abcdef", "name": "01191535581"},
    )
    assert resp.status_code == 400
    assert "name" not in seen


def test_register_with_real_name_succeeds_and_stores_trimmed_name(monkeypatch):
    client, seen = _register_client(monkeypatch)
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "covette84@example.com", "password": "abcdef", "name": "  양재혁 "},
    )
    assert resp.status_code == 200, resp.text
    assert seen["name"] == "양재혁"
    assert resp.json()["name"] == "양재혁"


def test_employee_list_label_shows_masked_email_next_to_name():
    from app.api import obys_workspaces as ws

    row = {"employee_name": "양재혁", "employee_email_masked": "c***@example.com"}
    assert ws._employee_label("employees", row) == "양재혁 (c***@example.com)"
    assert ws._employee_label("employees", {"employee_name": "양재혁"}) == "양재혁"
    assert ws._employee_label("attendance", row) == "양재혁"
