"""AADS-OBYS-TENANT-OWNER-APPROVED-CONTRACTS-GATE-20261022 — 신규 테넌트 대표의 승인 직원 목록·직원 계약 화면.

2026-10-22 운영 실측: 신규 테넌트 대표가 직원 가입요청을 승인한 뒤 '승인 직원 계약' 섹션이
GET /employees/approved·/contracts 를 레거시 게이트에 막혀(403) 계약서를 쓸 수 없었다.
여기서는 (1) 게이트가 화면이 부르는 (메서드, 경로)만 열고 나머지는 계속 닫아 두는지,
(2) 서비스가 비레거시 테넌트에서 관리자만, 호출자 테넌트 레코드만 다루는지,
(3) 레거시 테넌트·서명 경로가 그대로인지 고정한다.

오비서 DB 는 인메모리 가짜로 흉내 낸다 — 운영 DB·AADS 인증 DB 미접촉.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api  # noqa: E402
from app.core import obys_tenant  # noqa: E402

svc = api.svc

LEGACY = "15055cac-71b0-45ec-b714-7093dde189ff"
TENANT_A = "1bbc4f60-a1e6-462f-9a11-e3c52de4b739"
TENANT_B = "2a1b3c4d-0000-4000-8000-000000000001"
BIZ_A, BIZ_B, BIZ_STORE_A, BIZ_LEGACY = "biz-new-a", "biz-new-b", "biz-new-a-store", "biz-mia"
BASE = "/api/v1/yeoljeong-finance"
CONTRACT_ID = "2f6c1c0e-86d4-4f1d-9d52-0a9b8e7c1d3a"


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

MAPPING = {BIZ_A: TENANT_A, BIZ_STORE_A: TENANT_A, BIZ_B: TENANT_B, BIZ_LEGACY: LEGACY}
BUSINESS_INFO = {
    BIZ_A: {"name": "A 사업자", "branches": []},
    BIZ_STORE_A: {"name": "A 매장형", "branches": ["강남점"]},
    BIZ_B: {"name": "B 사업자", "branches": []},
}


class FakeDb:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {}
        self.upserts: list[tuple[str, dict]] = []
        self.mapping_unreadable = False


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS", "OBYS_CONTRACT_NOTIFY_CHANNELS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    state = FakeDb()

    async def fake_fetch_ledger(name, tenant_id=None):
        return [dict(r) for r in state.rows.get(name, []) if tenant_id and r.get("tenant_id") == tenant_id and not r.get("deleted_at")]

    async def fake_upsert_ledger(name, record):
        state.upserts.append((name, dict(record)))
        rows = state.rows.setdefault(name, [])
        for index, row in enumerate(rows):
            if row.get("id") == record.get("id"):
                if row.get("tenant_id") != record.get("tenant_id"):
                    return False
                rows[index] = dict(record)
                return True
        rows.append(dict(record))
        return True

    async def fake_business_tenant_matches(business_id, tenant_id):
        if state.mapping_unreadable:
            return None
        return MAPPING.get(business_id) == tenant_id

    async def fake_business_tenant_id(business_id):
        return MAPPING.get(business_id)

    async def fake_business_invite_info(business_id):
        return dict(BUSINESS_INFO[business_id]) if business_id in BUSINESS_INFO else None

    def run_db(coro):
        if coro.__name__.startswith("fake_"):
            return asyncio.run(coro)
        coro.close()
        return None

    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_run_db", run_db)
    monkeypatch.setattr(svc, "_db_fetch_ledger", fake_fetch_ledger)
    monkeypatch.setattr(svc, "_db_upsert_ledger", fake_upsert_ledger)
    monkeypatch.setattr(svc, "_db_business_tenant_matches", fake_business_tenant_matches)
    monkeypatch.setattr(svc, "_db_business_tenant_id", fake_business_tenant_id)
    monkeypatch.setattr(svc, "_db_business_invite_info", fake_business_invite_info)
    return state


def _employee(row_id: str, tenant_id: str, email: str, business_id: str, branch: str = "사무실", **extra) -> dict:
    return {
        "id": row_id, "tenant_id": tenant_id, "email": email, "name": f"직원-{row_id}", "status": "approved",
        "business_id": business_id, "branch": branch, "role": "employee", "phone": "010-1234-5678",
        "address": "서울시 어딘가 1", "birth_date": "1995-03-04", "requested_at": "2026-10-22T10:00:00+09:00", **extra,
    }


def _contract_payload(request_id: str, business_id: str, branch: str = "사무실", **extra) -> dict:
    return {
        "employee_request_id": request_id, "business_id": business_id, "branch": branch,
        "employer_name": "계약 사용자 상호", "employer_registration_no": "123-45-67890",
        "employer_representative": "대표자", "employer_address": "서울시 사용자 주소 2",
        "contract_type": "regular", "employment_tax_type": "four_insurance",
        "start_date": "2026-10-22", "contract_date": "2026-10-22", "wage_type": "monthly",
        "wage": 2800000, "workplace": "사무실", "job_description": "매장 운영",
        "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
        "work_days": "주 5일", "holidays": "매주 일요일", "pay_date": "매월 10일",
        "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용", **extra,
    }


def _client(user: dict) -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


@pytest.fixture
def seeded(db):
    db.rows["employee_join_requests"] = [
        _employee("jr-a1", TENANT_A, "emp-a1@example.com", BIZ_A),
        _employee("jr-a-store", TENANT_A, "emp-a2@example.com", BIZ_STORE_A, "강남점"),
        _employee("jr-a-pending", TENANT_A, "emp-a3@example.com", BIZ_A, status="pending"),
        _employee("jr-b1", TENANT_B, "emp-b1@example.com", BIZ_B),
        _employee("jr-l1", LEGACY, "emp-l1@example.com", BIZ_LEGACY, "열정국밥_미아점"),
    ]
    db.rows["contracts"] = [
        {"id": "ct-a-0001", "tenant_id": TENANT_A, "employee_email": "emp-a1@example.com", "business_id": BIZ_A,
         "status": "draft", "updated_at": "2026-10-22T11:00:00+09:00"},
        {"id": "ct-b-0001", "tenant_id": TENANT_B, "employee_email": "emp-b1@example.com", "business_id": BIZ_B,
         "status": "draft", "updated_at": "2026-10-22T11:00:00+09:00"},
        {"id": "ct-l-0001", "tenant_id": LEGACY, "employee_email": "emp-l1@example.com", "business_id": BIZ_LEGACY,
         "status": "draft", "updated_at": "2026-10-22T11:00:00+09:00"},
    ]
    return db


def _saved_contracts(state: FakeDb) -> list[dict]:
    return [row for name, row in state.upserts if name == "contracts"]


# --- 1. 게이트 ----------------------------------------------------------------------------------
def _request(path: str, method: str) -> SimpleNamespace:
    return SimpleNamespace(url=SimpleNamespace(path=path), method=method)


OPEN_ROUTES = [
    ("GET", f"{BASE}/employees/approved"),
    ("GET", f"{BASE}/contracts"),
    ("POST", f"{BASE}/contracts"),
    ("POST", f"{BASE}/contracts/{CONTRACT_ID}/request-signature"),
    ("POST", f"{BASE}/contracts/{CONTRACT_ID}/resend-signature-notice"),
    ("GET", f"{BASE}/contracts/{CONTRACT_ID}/signed-pdf"),
    ("POST", f"{BASE}/contracts/{CONTRACT_ID}/signed-pdf/regenerate"),
    ("GET", f"{BASE}/onboarding/document-types"),
    ("GET", f"{BASE}/onboarding/documents"),
    ("POST", f"{BASE}/onboarding/documents"),
    ("GET", f"{BASE}/onboarding/documents/{CONTRACT_ID}/download"),
    ("GET", f"{BASE}/business-documents"),
    ("POST", f"{BASE}/business-documents"),
]

CLOSED_ROUTES = [
    ("DELETE", f"{BASE}/contracts/{CONTRACT_ID}"),
    ("PUT", f"{BASE}/contracts"),
    ("PATCH", f"{BASE}/contracts"),
    ("DELETE", f"{BASE}/contracts"),
    ("GET", f"{BASE}/contracts/{CONTRACT_ID}"),
    ("GET", f"{BASE}/contracts/{CONTRACT_ID}/request-signature"),
    ("POST", f"{BASE}/contracts/{CONTRACT_ID}/signed-pdf"),
    ("POST", f"{BASE}/contracts/{CONTRACT_ID}/request-signature/x"),
    ("GET", f"{BASE}/contracts/signing-extra"),
    ("POST", f"{BASE}/employees/approved"),
    ("GET", f"{BASE}/employees/approved/"),
    ("GET", f"{BASE}/employees/approved/abc/role"),
    ("PATCH", f"{BASE}/employees/approved/abc/role"),
    ("PATCH", f"{BASE}/employees/approved/abc/assignment"),
    ("POST", f"{BASE}/employees/approved/abc/resync-employment"),
    ("GET", f"{BASE}/employees"),
    ("GET", f"{BASE}/onboarding/documents/{CONTRACT_ID}"),
    ("DELETE", f"{BASE}/onboarding/documents/{CONTRACT_ID}"),
    ("POST", f"{BASE}/onboarding/document-types"),
    ("GET", f"{BASE}/business-documents/{CONTRACT_ID}/download"),
    ("PATCH", f"{BASE}/business-documents/{CONTRACT_ID}"),
    ("DELETE", f"{BASE}/business-documents/{CONTRACT_ID}"),
    ("PUT", f"{BASE}/business-documents"),
    ("GET", f"{BASE}/payroll"),
]


@pytest.mark.parametrize("method,path", OPEN_ROUTES)
async def test_gate_opens_owner_contract_screen_routes_for_new_tenant(method, path):
    user = {"tenant_id": TENANT_A}
    assert await obys_tenant.require_legacy_obys_access(_request(path, method), user) is user


@pytest.mark.parametrize("method,path", CLOSED_ROUTES)
async def test_gate_keeps_everything_else_closed_for_new_tenant(method, path):
    with pytest.raises(HTTPException) as excinfo:
        await obys_tenant.require_legacy_obys_access(_request(path, method), {"tenant_id": TENANT_A})
    assert excinfo.value.status_code == 403


@pytest.mark.parametrize("method,path", OPEN_ROUTES + CLOSED_ROUTES)
async def test_gate_for_legacy_tenant_is_unchanged(method, path):
    user = {"tenant_id": LEGACY}
    assert await obys_tenant.require_legacy_obys_access(_request(path, method), user) is user


@pytest.mark.parametrize("tenant", ["", None])
async def test_gate_requires_a_tenant_id_even_for_open_routes(tenant):
    with pytest.raises(HTTPException) as excinfo:
        await obys_tenant.require_legacy_obys_access(_request(f"{BASE}/contracts", "GET"), {"tenant_id": tenant})
    assert excinfo.value.status_code == 403


async def test_signing_paths_stay_open_for_new_tenant_through_the_gate():
    token = "B" * 32
    for method, path in (
        ("GET", f"{BASE}/contracts/signing/{token}"),
        ("POST", f"{BASE}/contracts/signing"),
        ("GET", f"{BASE}/contracts/pending-signature"),
        ("POST", f"{BASE}/contracts/signing-renewal"),
        ("GET", f"{BASE}/contracts/{CONTRACT_ID}/signing-view"),
    ):
        user = {"tenant_id": TENANT_A}
        assert await obys_tenant.require_legacy_obys_access(_request(path, method), user) is user


# --- 2. 신규 테넌트 대표 -------------------------------------------------------------------------
def test_new_tenant_owner_lists_only_own_approved_employees(seeded):
    response = _client(OWNER_A).get(f"{BASE}/employees/approved")
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()["employees"]} == {"jr-a1", "jr-a-store"}
    other = _client(OWNER_B).get(f"{BASE}/employees/approved")
    assert [row["id"] for row in other.json()["employees"]] == ["jr-b1"]


def test_new_tenant_approved_employees_keep_db_registered_business_id(seeded):
    rows = {row["id"]: row for row in _client(OWNER_A).get(f"{BASE}/employees/approved").json()["employees"]}
    assert rows["jr-a1"]["business_id"] == BIZ_A
    assert rows["jr-a-store"]["business_id"] == BIZ_STORE_A
    assert rows["jr-a1"]["needs_contract"] is False
    assert rows["jr-a-store"]["needs_contract"] is True


def test_new_tenant_owner_lists_only_own_contracts(seeded):
    response = _client(OWNER_A).get(f"{BASE}/contracts")
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["contracts"]] == ["ct-a-0001"]
    other = _client(OWNER_B).get(f"{BASE}/contracts")
    assert [row["id"] for row in other.json()["contracts"]] == ["ct-b-0001"]


def test_new_tenant_owner_creates_contract_for_own_office_employee(seeded):
    response = _client(OWNER_A).post(f"{BASE}/contracts", json=_contract_payload("jr-a1", BIZ_A))
    assert response.status_code == 200, response.text
    contract = response.json()["contract"]
    assert contract["tenant_id"] == TENANT_A
    assert contract["business_id"] == BIZ_A
    assert contract["employee_email"] == "emp-a1@example.com"
    assert contract["status"] == "draft"
    saved = _saved_contracts(seeded)
    assert saved and all(row["tenant_id"] == TENANT_A for row in saved)


def test_new_tenant_owner_creates_contract_for_own_store_employee(seeded):
    response = _client(OWNER_A).post(
        f"{BASE}/contracts", json=_contract_payload("jr-a-store", BIZ_STORE_A, "강남점", workplace="강남점")
    )
    assert response.status_code == 200, response.text
    assert response.json()["contract"]["business_id"] == BIZ_STORE_A


def test_store_contract_rejects_branch_not_in_business(seeded):
    response = _client(OWNER_A).post(
        f"{BASE}/contracts", json=_contract_payload("jr-a-store", BIZ_STORE_A, "없는지점")
    )
    assert response.status_code == 400
    assert not _saved_contracts(seeded)


def test_owner_requests_signature_only_on_own_contract(seeded):
    created = svc.save_contract(_contract_payload("jr-a1", BIZ_A), OWNER_A)
    requested = svc.request_contract_signature(created["id"], OWNER_A)
    assert requested["status"] == "requested" and requested["tenant_id"] == TENANT_A
    with pytest.raises(HTTPException) as excinfo:
        svc.request_contract_signature(created["id"], OWNER_B)
    assert excinfo.value.status_code == 404


def test_new_tenant_owner_reads_onboarding_documents_of_own_tenant_only(seeded):
    seeded.rows["onboarding_documents"] = [
        {"id": "doc-a-0001", "tenant_id": TENANT_A, "employee_email": "emp-a1@example.com", "business_id": BIZ_A,
         "document_type": "id_card", "status": "uploaded", "employee_request_id": "jr-a1"},
        {"id": "doc-b-0001", "tenant_id": TENANT_B, "employee_email": "emp-b1@example.com", "business_id": BIZ_B,
         "document_type": "id_card", "status": "uploaded", "employee_request_id": "jr-b1"},
    ]
    response = _client(OWNER_A).get(f"{BASE}/onboarding/documents")
    assert response.status_code == 200, response.text
    ids = {row["id"] for row in response.json()["documents"]}
    assert "doc-a-0001" in ids and "doc-b-0001" not in ids
    types = _client(OWNER_A).get(f"{BASE}/onboarding/document-types")
    assert types.status_code == 200 and types.json()["document_types"]


def test_business_documents_fail_closed_without_db_for_new_tenant(seeded, monkeypatch):
    monkeypatch.setattr(svc, "_db_available", lambda: False)
    response = _client(OWNER_A).get(f"{BASE}/business-documents", params={"business_id": BIZ_A})
    assert response.status_code == 403


# --- 3. 타 테넌트 대표 ---------------------------------------------------------------------------
def test_other_tenant_owner_cannot_create_contract_for_foreign_employee(seeded):
    response = _client(OWNER_B).post(f"{BASE}/contracts", json=_contract_payload("jr-a1", BIZ_A))
    assert response.status_code == 403
    assert not _saved_contracts(seeded)


@pytest.mark.parametrize("business_id", [BIZ_B, BIZ_LEGACY, "biz-unmapped"])
def test_owner_cannot_use_own_employee_with_foreign_business(seeded, business_id):
    response = _client(OWNER_A).post(f"{BASE}/contracts", json=_contract_payload("jr-a1", business_id))
    assert response.status_code == 403, response.text
    assert not _saved_contracts(seeded)


def test_unknown_employee_gets_same_403_as_foreign_employee(seeded):
    unknown = _client(OWNER_A).post(f"{BASE}/contracts", json=_contract_payload("jr-nonexistent", BIZ_A))
    foreign = _client(OWNER_A).post(f"{BASE}/contracts", json=_contract_payload("jr-b1", BIZ_A))
    assert unknown.status_code == foreign.status_code == 403
    assert unknown.json() == foreign.json()


def test_owner_cannot_overwrite_foreign_contract_by_id(seeded):
    response = _client(OWNER_A).post(f"{BASE}/contracts", json=_contract_payload("jr-a1", BIZ_A, id="ct-b-0001"))
    assert response.status_code != 200
    row = next(r for r in seeded.rows["contracts"] if r["id"] == "ct-b-0001")
    assert row["tenant_id"] == TENANT_B and row["status"] == "draft"


def test_mapping_unreadable_closes_contract_creation(seeded):
    seeded.mapping_unreadable = True
    response = _client(OWNER_A).post(f"{BASE}/contracts", json=_contract_payload("jr-a1", BIZ_A))
    assert response.status_code == 403
    assert not _saved_contracts(seeded)


def test_db_read_failure_returns_nothing_instead_of_other_tenants_rows(seeded, monkeypatch):
    def broken(coro):
        coro.close()
        return None

    monkeypatch.setattr(svc, "_run_db", broken)
    assert svc.list_approved_employees(OWNER_A) == []
    assert svc.list_contracts(OWNER_A) == []


# --- 4. 신규 테넌트 member(직원) ------------------------------------------------------------------
def test_new_tenant_member_gets_403_on_every_opened_owner_route(seeded):
    client = _client(MEMBER_A)
    assert client.get(f"{BASE}/employees/approved").status_code == 403
    assert client.get(f"{BASE}/contracts").status_code == 403
    assert client.post(f"{BASE}/contracts", json=_contract_payload("jr-a1", BIZ_A)).status_code == 403
    assert client.post(f"{BASE}/contracts/ct-a-0001/request-signature").status_code == 403
    assert client.get(f"{BASE}/contracts/ct-a-0001/signed-pdf").status_code == 403
    assert client.get(f"{BASE}/onboarding/documents").status_code == 403
    assert client.get(f"{BASE}/business-documents").status_code == 403
    assert not _saved_contracts(seeded)


# --- 5. 레거시 테넌트 동작 불변 ---------------------------------------------------------------------
def test_legacy_owner_keeps_listing_contracts_and_employees(seeded):
    client = _client(LEGACY_OWNER)
    employees = client.get(f"{BASE}/employees/approved")
    assert employees.status_code == 200
    assert [row["id"] for row in employees.json()["employees"]] == ["jr-l1"]
    contracts = client.get(f"{BASE}/contracts")
    assert [row["id"] for row in contracts.json()["contracts"]] == ["ct-l-0001"]


def test_legacy_owner_foreign_employee_keeps_previous_non_403_rejection(seeded):
    response = _client(LEGACY_OWNER).post(f"{BASE}/contracts", json=_contract_payload("jr-a1", BIZ_A))
    assert response.status_code == 400


def test_require_owner_path_admin_lets_legacy_member_through():
    # 레거시 호출자는 종전 판정(역할 무관 통과 후 서비스 내부 판정)을 그대로 탄다.
    svc._require_owner_path_admin({**LEGACY_OWNER, "tenant_role": "member"}, "x")
    with pytest.raises(HTTPException) as excinfo:
        svc._require_owner_path_admin(MEMBER_A, "x")
    assert excinfo.value.status_code == 403
