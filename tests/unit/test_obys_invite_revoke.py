"""AADS-OBYS-INVITE-REVOKE-20261001 — 직원 초대 취소(소프트 삭제) + 초대 목록 테넌트 필터.

초대를 만들면 되돌릴 방법이 없었다. 여기서는 (1) 관리자가 자기 테넌트 초대를 취소하면 status=revoked·token 제거,
(2) 비관리자·다른 테넌트·수락된 초대의 거부, (3) 취소된 초대의 resolve/accept 가 404 (링크가 실제로 죽는지),
(4) list_invites 가 다른 테넌트 초대를 돌려주지 않는지, (5) API 라우트와 화면 연결을 고정한다.

오비서 DB 는 인메모리 가짜로 흉내 낸다 — 운영 DB·AADS 인증 DB 미접촉. 가짜 패턴은 test_obys_invite_multistore.py 와 같다.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api  # noqa: E402

svc = api.svc

EMPLOYER = "15055cac-71b0-45ec-b714-7093dde189ff"
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"
NEW_EMP_TENANT = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"
OTHER_BIZ = "biz-other-tenant"
EMP_EMAIL = "new-hire@example.com"


def _user(tenant_id: str, role: str, *, email: str, user_id: str, **extra) -> dict:
    return {
        "user_id": user_id,
        "email": email,
        "is_admin": False,
        "tenant_id": tenant_id,
        "tenant_role": role,
        "user_role": "user",
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": role},
        **extra,
    }


ADMIN = _user(EMPLOYER, "owner", email="owner@example.com", user_id="user-owner")
MEMBER = _user(EMPLOYER, "member", email="m@example.com", user_id="user-m")
OTHER_ADMIN = _user(OTHER_TENANT, "owner", email="other-owner@example.com", user_id="user-other-owner")
EMPLOYEE = _user(NEW_EMP_TENANT, "owner", email=EMP_EMAIL, user_id="user-new-hire")

BUSINESS_NAMES = {"biz-sungshin": "열정국밥 성신여대점", "biz-mia": "열정국밥_미아점", OTHER_BIZ: "남의 사업자"}
BRANCHES = {"biz-sungshin": ["성신여대점"], "biz-mia": ["열정국밥_미아점"], OTHER_BIZ: ["타지점"]}
MAPPING = {"biz-sungshin": EMPLOYER, "biz-mia": EMPLOYER, OTHER_BIZ: OTHER_TENANT}


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
        assert name == "employee_join_requests"
        join_rows.append(dict(record))
        return True

    async def fake_business_tenant_id(business_id):
        return MAPPING.get(business_id)

    async def fake_business_tenant_matches(business_id, tenant_id):
        return MAPPING.get(business_id) == tenant_id

    async def fake_hr_record_tenant(name, row_id):
        return next((r["tenant_id"] for r in join_rows if r["id"] == row_id), None)

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
    monkeypatch.setattr(svc, "_db_business_invite_info", fake_business_invite_info)
    return join_rows


def _invite(**payload) -> dict:
    return svc.create_invite({"name": "양재혁", "phone": "010-1234-5678", **payload}, ADMIN)


def _stored(invite_id: str) -> dict:
    return svc._find(svc._read_file_rows("employee_invites"), invite_id)


def _plant_other_tenant_invite() -> None:
    # create_invite 는 호출자 테넌트 사업자만 허용하므로 다른 테넌트 초대는 파일에 직접 심는다.
    svc._write_file_rows("employee_invites", [
        *svc._read_file_rows("employee_invites"),
        {"id": "inv-other", "token": "tok-other", "name": "남의 직원", "status": "pending", "created_at": "2026-10-01T00:00:00+09:00",
         "targets": [{"business_id": OTHER_BIZ, "branch": "타지점"}]},
    ])


# --- 1. 관리자가 자기 테넌트 초대를 취소 ---------------------------------------------------
def test_admin_revokes_own_tenant_invite(db):
    invite = _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}])
    view = svc.revoke_invite(invite["id"], ADMIN)
    assert view["status"] == "revoked"
    assert view["revoked_by"] == "owner@example.com"
    assert view["revoked_at"]
    assert "token" not in view
    stored = _stored(invite["id"])
    assert stored["status"] == "revoked"
    assert stored["revoked_at"] and stored["revoked_by"] == "owner@example.com"
    assert "token" not in stored
    assert stored["targets"] == invite["targets"]  # 소프트 취소: 레코드는 남는다


# --- 2~4. 거부 경로 -----------------------------------------------------------------------
def test_non_admin_cannot_revoke(db):
    invite = _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}])
    with pytest.raises(HTTPException) as excinfo:
        svc.revoke_invite(invite["id"], MEMBER)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "직원 초대 취소 권한이 없습니다"
    assert _stored(invite["id"])["status"] == "pending"
    assert _stored(invite["id"])["token"] == invite["token"]


def test_other_tenant_invite_cannot_be_revoked(db):
    _plant_other_tenant_invite()
    with pytest.raises(HTTPException) as excinfo:
        svc.revoke_invite("inv-other", ADMIN)
    assert excinfo.value.status_code == 403
    assert _stored("inv-other")["status"] == "pending"
    assert _stored("inv-other")["token"] == "tok-other"
    # 소유 테넌트 관리자는 취소할 수 있다.
    assert svc.revoke_invite("inv-other", OTHER_ADMIN)["status"] == "revoked"


def test_unknown_invite_id_is_404(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.revoke_invite("no-such-id", ADMIN)
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "초대를 찾을 수 없습니다"


# --- 5. 수락된 초대 -----------------------------------------------------------------------
def test_accepted_invite_cannot_be_revoked(db):
    invite = _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}])
    svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert _stored(invite["id"])["status"] == "accepted"
    with pytest.raises(HTTPException) as excinfo:
        svc.revoke_invite(invite["id"], ADMIN)
    assert excinfo.value.status_code == 409
    assert "가입요청을 반려" in excinfo.value.detail
    assert _stored(invite["id"])["status"] == "accepted"


# --- 6. 멱등 ------------------------------------------------------------------------------
def test_revoking_twice_is_idempotent(db):
    invite = _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}])
    first = svc.revoke_invite(invite["id"], ADMIN)
    second = svc.revoke_invite(invite["id"], ADMIN)
    assert second["status"] == "revoked"
    assert second["revoked_at"] == first["revoked_at"]  # 두 번째 호출이 취소 기록을 덮어쓰지 않는다
    assert len(svc._read_file_rows("employee_invites")) == 1


# --- 7. 취소된 초대의 링크는 죽는다 (가장 중요) --------------------------------------------------
def test_revoked_invite_link_is_dead_for_resolve_and_accept(db):
    invite = _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}])
    token = invite["token"]
    assert svc.resolve_invite(token)["id"] == invite["id"]  # 취소 전에는 열린다
    svc.revoke_invite(invite["id"], ADMIN)
    with pytest.raises(HTTPException) as resolve_error:
        svc.resolve_invite(token)
    assert resolve_error.value.status_code == 404
    with pytest.raises(HTTPException) as accept_error:
        svc.accept_invite({"token": token}, EMPLOYEE)
    assert accept_error.value.status_code == 404
    assert db == []  # 가입요청이 만들어지지 않았다
    assert _stored(invite["id"])["status"] == "revoked"


def test_revoked_status_with_surviving_token_is_still_rejected(db):
    # 토큰이 남은 revoked 레코드(다른 경로·수동 편집)도 _find_invite 가 막는다.
    svc._write_file_rows("employee_invites", [{
        "id": "inv-r", "token": "tok-r", "status": "revoked",
        "targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}],
    }])
    for call in (lambda: svc.resolve_invite("tok-r"), lambda: svc.accept_invite({"token": "tok-r"}, EMPLOYEE)):
        with pytest.raises(HTTPException) as excinfo:
            call()
        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "취소된 초대입니다"
    assert db == []


# --- 8. list_invites 테넌트 필터 ------------------------------------------------------------
def test_list_invites_hides_other_tenant_invites(db):
    mine = _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}])
    _plant_other_tenant_invite()
    assert [row["id"] for row in svc.list_invites(ADMIN)] == [mine["id"]]
    assert [row["id"] for row in svc.list_invites(OTHER_ADMIN)] == ["inv-other"]
    assert svc.list_invites(MEMBER) == []


def test_list_invites_keeps_legacy_invites_without_resolvable_tenant(db):
    # 사업자 매핑이 없는 예전 초대는 테넌트를 특정할 수 없으므로 종전처럼 보인다.
    svc._write_file_rows("employee_invites", [
        {"id": "inv-legacy", "token": "tok-legacy", "name": "예전", "branch": "알수없는지점", "status": "pending", "created_at": "2020-01-01T00:00:00+09:00"},
    ])
    assert [row["id"] for row in svc.list_invites(ADMIN)] == ["inv-legacy"]
    assert svc.revoke_invite("inv-legacy", ADMIN)["status"] == "revoked"


def test_list_invites_shows_revoked_rows_with_status(db):
    invite = _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}])
    svc.revoke_invite(invite["id"], ADMIN)
    listed = svc.list_invites(ADMIN)
    assert [(row["id"], row["status"]) for row in listed] == [(invite["id"], "revoked")]


def test_internal_admin_sees_all_tenants(db):
    _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}])
    _plant_other_tenant_invite()
    internal = _user(EMPLOYER, "owner", email="ceo@example.com", user_id="user-ceo", user_role="ceo")
    assert len(svc.list_invites(internal)) == 2


# --- 9. API 라우트·화면 ----------------------------------------------------------------------
def test_delete_route_is_registered_without_shadowing_resolve_and_accept():
    routes = {(method, route.path) for route in api.router.routes for method in getattr(route, "methods", set())}
    base = "/employees/invites"
    assert ("DELETE", f"{base}/{{invite_id}}") in {(m, p.split("yeoljeong-finance", 1)[-1]) for m, p in routes}
    assert ("GET", f"{base}/resolve") in {(m, p.split("yeoljeong-finance", 1)[-1]) for m, p in routes}
    assert ("POST", f"{base}/accept") in {(m, p.split("yeoljeong-finance", 1)[-1]) for m, p in routes}


def test_v41_screen_has_revoke_button_and_status_badge():
    html = Path("app/static/apps/obys/mockup-v4-1.html").read_text(encoding="utf-8")
    assert "<th>상태</th><th>관리</th>" in html
    assert 'data-invite-revoke="${escapeHtml(invite.id)}"' in html
    assert 'status==="revoked")return["취소","bad"]' in html
    assert "async function revokeInviteV41(inviteId)" in html
    assert "이 초대를 취소할까요? 보낸 링크는 더 이상 쓸 수 없습니다." in html
    assert '"/employees/invites/"+encodeURIComponent(inviteId),{method:"DELETE"}' in html
    assert "직원 초대 취소는 대표·운영관리자만 할 수 있습니다." in html
    # 수락된 초대에는 취소 버튼을 그리지 않는다.
    assert 'toLowerCase()==="accepted"?""' in html
