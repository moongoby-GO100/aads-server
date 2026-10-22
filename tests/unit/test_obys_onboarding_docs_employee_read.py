"""OBYS-M1-EMPLOYEE-ONBOARDING-DOCS-403-20261022 — 직원 화면 입사서류 목록·다운로드.

adc72730 가 넣은 _require_owner_path_admin 가드가 비레거시 테넌트의 승인 직원(member)까지 403 으로 막아
직원 화면에 "서류 조회 실패" 가 떴다.  승인된 직원 본인에게만 본인 서류 조회·다운로드를 열고,
미승인 member·직원 기록 없는 사용자·타 테넌트는 종전처럼 막는다.  오비서 DB 는 인메모리 가짜 — 운영 DB 미접촉.
"""
from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api  # noqa: E402

svc = api.svc

LEGACY = "15055cac-71b0-45ec-b714-7093dde189ff"
TENANT_A = "1bbc4f60-a1e6-462f-9a11-e3c52de4b739"
TENANT_B = "2a1b3c4d-0000-4000-8000-000000000001"
BIZ_A, BIZ_B = "biz-new-a", "biz-new-b"
BASE = "/api/v1/yeoljeong-finance"
MAPPING = {BIZ_A: TENANT_A, BIZ_B: TENANT_B}


def _user(tenant_id: str, role: str, email: str) -> dict:
    return {
        "user_id": f"u-{email}",
        "email": email,
        "is_admin": False,
        "tenant_id": tenant_id,
        "tenant_role": role,
        "user_role": "user",
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": role},
    }


OWNER_A = _user(TENANT_A, "owner", "owner-a@example.com")
STAFF1 = _user(TENANT_A, "member", "emp-a1@example.com")
STAFF2 = _user(TENANT_A, "member", "emp-a2@example.com")
PENDING = _user(TENANT_A, "member", "emp-a3@example.com")
REJECTED = _user(TENANT_A, "member", "emp-a4@example.com")
STRANGER = _user(TENANT_A, "member", "nobody@example.com")
STAFF_B = _user(TENANT_B, "member", "emp-b1@example.com")


def _employee(row_id: str, tenant_id: str, email: str, business_id: str, status: str = "approved", **extra) -> dict:
    return {
        "id": row_id, "tenant_id": tenant_id, "email": email, "name": f"직원-{row_id}", "status": status,
        "business_id": business_id, "branch": "사무실", "role": "employee",
        "requested_at": "2026-10-22T10:00:00+09:00", **extra,
    }


def _document(doc_id: str, tenant_id: str, email: str, business_id: str, filename: str) -> dict:
    return {
        "id": doc_id, "tenant_id": tenant_id, "employee_email": email, "uploaded_by": email, "business_id": business_id,
        "branch": "사무실", "document_type": "id_card", "status": "uploaded", "original_filename": filename,
        "stored_filename": filename, "content_type": "text/plain", "uploaded_at": "2026-10-22T11:00:00+09:00",
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    upload_dir = tmp_path / "data" / "uploads" / "onboarding"
    upload_dir.mkdir(parents=True)
    for name, body in (("a1.txt", b"a1-secret"), ("a2.txt", b"a2-secret"), ("b1.txt", b"b1-secret")):
        (upload_dir / name).write_bytes(body)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", upload_dir)
    rows: dict[str, list[dict]] = {
        "employee_join_requests": [
            _employee("jr-a1", TENANT_A, STAFF1["email"], BIZ_A),
            _employee("jr-a2", TENANT_A, STAFF2["email"], BIZ_A),
            _employee("jr-a3", TENANT_A, PENDING["email"], BIZ_A, status="pending"),
            _employee("jr-a4", TENANT_A, REJECTED["email"], BIZ_A, status="rejected"),
            _employee("jr-b1", TENANT_B, STAFF_B["email"], BIZ_B),
        ],
        "onboarding_documents": [
            _document("doc-a1-0001", TENANT_A, STAFF1["email"], BIZ_A, "a1.txt"),
            _document("doc-a2-0001", TENANT_A, STAFF2["email"], BIZ_A, "a2.txt"),
            _document("doc-b1-0001", TENANT_B, STAFF_B["email"], BIZ_B, "b1.txt"),
        ],
    }

    async def fake_fetch_ledger(name, tenant_id=None):
        return [dict(r) for r in rows.get(name, []) if tenant_id and r.get("tenant_id") == tenant_id and not r.get("deleted_at")]

    async def fake_business_tenant_matches(business_id, tenant_id):
        return MAPPING.get(business_id) == tenant_id

    def run_db(coro):
        if coro.__name__.startswith("fake_"):
            return asyncio.run(coro)
        coro.close()
        return None

    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_run_db", run_db)
    monkeypatch.setattr(svc, "_db_fetch_ledger", fake_fetch_ledger)
    monkeypatch.setattr(svc, "_db_business_tenant_matches", fake_business_tenant_matches)
    return rows


def _client(user: dict) -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


def _listed(user: dict, **params) -> list[dict]:
    response = _client(user).get(f"{BASE}/onboarding/documents", params=params)
    assert response.status_code == 200, response.text
    return response.json()["documents"]


def test_approved_employee_sees_only_own_documents(db):
    rows = _listed(STAFF1)
    real = [r for r in rows if not r.get("is_placeholder")]
    assert [r["id"] for r in real] == ["doc-a1-0001"]
    assert {r["employee_email"] for r in rows} == {STAFF1["email"]}
    assert not any("a2" in r["id"] or "b1" in r["id"] for r in rows)


def test_approved_employee_gets_own_missing_rows_only(db):
    missing = [r for r in _listed(STAFF1) if r.get("is_placeholder")]
    assert missing
    assert {r["employee_email"] for r in missing} == {STAFF1["email"]}
    assert {r["employee_request_id"] for r in missing} == {"jr-a1"}


def test_business_id_filter_cannot_widen_or_expose_other_rows(db):
    rows = _listed(STAFF1, business_id=BIZ_B)
    assert {r["employee_email"] for r in rows} <= {STAFF1["email"]}
    assert all(r.get("business_id") != BIZ_B for r in rows)


@pytest.mark.parametrize("user", [PENDING, REJECTED, STRANGER])
def test_unapproved_or_unknown_member_still_403(db, user):
    assert _client(user).get(f"{BASE}/onboarding/documents").status_code == 403
    assert _client(user).get(f"{BASE}/onboarding/documents/doc-a1-0001/download").status_code == 403


def test_employee_of_other_tenant_never_sees_this_tenants_rows(db):
    rows = _listed(STAFF_B)
    assert {r["employee_email"] for r in rows} == {STAFF_B["email"]}
    assert all(r["id"] != "doc-a1-0001" for r in rows)
    assert _client(STAFF_B).get(f"{BASE}/onboarding/documents/doc-a1-0001/download").status_code == 404


def test_tenant_a_employee_cannot_read_tenant_b_document(db):
    assert _client(STAFF1).get(f"{BASE}/onboarding/documents/doc-b1-0001/download").status_code == 404


def test_member_with_only_other_tenant_request_is_403(db):
    outsider = _user(TENANT_A, "member", STAFF_B["email"])
    assert _client(outsider).get(f"{BASE}/onboarding/documents").status_code == 403


def test_owner_listing_is_unchanged(db):
    rows = _listed(OWNER_A)
    assert {r["id"] for r in rows if not r.get("is_placeholder")} == {"doc-a1-0001", "doc-a2-0001"}
    assert {r["employee_email"] for r in rows} >= {STAFF1["email"], STAFF2["email"]}


def test_owner_can_download_any_tenant_document_and_only_own_tenant(db):
    ok = _client(OWNER_A).get(f"{BASE}/onboarding/documents/doc-a2-0001/download")
    assert ok.status_code == 200 and ok.content == b"a2-secret"
    assert _client(OWNER_A).get(f"{BASE}/onboarding/documents/doc-b1-0001/download").status_code == 404


def test_employee_downloads_own_document(db):
    response = _client(STAFF1).get(f"{BASE}/onboarding/documents/doc-a1-0001/download")
    assert response.status_code == 200
    assert response.content == b"a1-secret"


def test_employee_download_of_other_employees_document_is_404(db):
    response = _client(STAFF1).get(f"{BASE}/onboarding/documents/doc-a2-0001/download")
    missing = _client(STAFF1).get(f"{BASE}/onboarding/documents/doc-zzzz-9999/download")
    assert response.status_code == 404
    assert response.json() == missing.json()


def test_employee_still_cannot_use_management_actions(db):
    client = _client(STAFF1)
    assert client.patch(f"{BASE}/onboarding/documents/doc-a1-0001/review", json={"status": "approved"}).status_code == 403
    assert client.post(f"{BASE}/onboarding/documents/doc-a1-0001/bankbook-extract").status_code == 403
    assert db["onboarding_documents"][0]["status"] == "uploaded"


def test_latest_non_approved_request_closes_access(db):
    db["employee_join_requests"].append(
        _employee("jr-a1-new", TENANT_A, STAFF1["email"], BIZ_A, status="pending", updated_at="2026-10-23T09:00:00+09:00")
    )
    with pytest.raises(HTTPException) as excinfo:
        svc.list_onboarding_documents(STAFF1)
    assert excinfo.value.status_code == 403


def test_legacy_guard_semantics_unchanged():
    svc._require_owner_path_admin({**_user(LEGACY, "member", "x@example.com")}, "x")
    with pytest.raises(HTTPException) as excinfo:
        svc._require_owner_path_admin(STAFF1, "x")
    assert excinfo.value.status_code == 403
