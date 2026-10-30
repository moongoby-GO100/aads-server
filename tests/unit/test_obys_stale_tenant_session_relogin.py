"""소속이 정지된 JWT 가 403 에 갇히지 않고 재로그인으로 복구되는지 고정한다.

2026-10-14 실측: owner membership 을 suspended 로 바꾸기 전에 발급된 JWT 는 모든 API 가
403 이어서 화면이 로그인으로 가지 않았다.  세션 자체가 무효인 경우만 401 + 코드로 바꾸고,
역할 부족·타 조직 명시 요청 같은 진짜 권한 오류는 403 으로 남긴다.
"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import app.auth as auth_module
from app.auth import TenantRole, get_current_user, require_tenant_role

TENANT_OLD = "0255b87e-0000-0000-0000-000000000001"
TENANT_NEW = "15055cac-0000-0000-0000-000000000002"
INDEX_HTML = Path(__file__).resolve().parents[2] / "app/static/apps/obys/index.html"


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    async def fetchval(self, *args, **kwargs):
        return None

    async def fetchrow(self, query, user_id, tenant_id):
        return self._rows.get(tenant_id)


class _Pool:
    def __init__(self, rows):
        self._rows = rows

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self._rows)


def _row(tenant_id, role="member"):
    return {
        "tenant_id": tenant_id, "slug": "s", "name": "N", "kind": "customer",
        "tenant_status": "active", "membership_id": "m", "role": role,
        "membership_status": "active", "user_email": "u@example.test", "user_role": "user",
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth_module, "SECRET_KEY", "test-secret-key-for-stale-tenant-session-0123456789")
    rows = {TENANT_NEW: _row(TENANT_NEW, "member")}  # TENANT_OLD 는 suspended → 조회 안 됨

    async def _ready():
        return None

    async def _pool():
        return _Pool(rows)

    async def _state(user_id):
        return {"must_change_password": False, "session_version": 0}

    monkeypatch.setattr(auth_module, "require_saas_schema_ready", _ready)
    monkeypatch.setattr(auth_module, "_ensure_pool", _pool)
    monkeypatch.setattr(auth_module, "load_session_state", _state)

    app = FastAPI()

    @app.get("/me")
    async def me(user: dict = Depends(get_current_user)):
        return {"tenant_id": user["tenant_id"]}

    @app.get("/admin-only")
    async def admin_only(ctx: dict = Depends(require_tenant_role(TenantRole.ADMIN))):
        return {"ok": True}

    return TestClient(app)


def _auth(tenant_id, extra=None):
    token = auth_module.create_token("7", "u@example.test", tenant_id=tenant_id)
    headers = {"Authorization": f"Bearer {token}"}
    headers.update(extra or {})
    return headers


def test_suspended_membership_jwt_is_401_with_code(client):
    r = client.get("/me", headers=_auth(TENANT_OLD))
    assert r.status_code == 401
    assert r.headers["X-Auth-Error-Code"] == "tenant_membership_inactive"
    assert "tenant_membership_inactive" in r.json()["detail"]
    assert "다시 로그인" in r.json()["detail"]


def test_active_membership_jwt_is_200(client):
    r = client.get("/me", headers=_auth(TENANT_NEW))
    assert r.status_code == 200
    assert r.json()["tenant_id"] == TENANT_NEW


def test_insufficient_role_stays_403(client):
    r = client.get("/admin-only", headers=_auth(TENANT_NEW))
    assert r.status_code == 403
    assert "X-Auth-Error-Code" not in r.headers


def test_explicit_other_tenant_header_stays_403(client):
    r = client.get("/me", headers=_auth(TENANT_NEW, {"X-Tenant-ID": TENANT_OLD}))
    assert r.status_code == 403
    assert "X-Auth-Error-Code" not in r.headers


def test_header_equal_to_token_tenant_is_session_invalid(client):
    r = client.get("/me", headers=_auth(TENANT_OLD, {"X-Tenant-ID": TENANT_OLD}))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_skips_suspended_default_tenant(monkeypatch):
    async def list_tenants(user_id):
        return [
            {"tenant_id": TENANT_OLD, "kind": "customer", "status": "suspended"},
            {"tenant_id": TENANT_NEW, "kind": "customer", "status": "active"},
        ]

    async def ensure_customer(**kwargs):
        return {"tenant_id": TENANT_NEW}

    monkeypatch.setattr(auth_module, "list_user_tenants", list_tenants)
    monkeypatch.setattr(auth_module, "ensure_customer_tenant_for_user", ensure_customer)
    monkeypatch.setattr(auth_module, "_is_internal_tenant_principal", lambda email, role: False)
    tenant_id = await auth_module.resolve_login_tenant_for_user(
        {"id": "u-1", "email": "u@example.test", "role": "user", "default_tenant_id": TENANT_OLD}
    )
    assert tenant_id == TENANT_NEW


def _html():
    return INDEX_HTML.read_text(encoding="utf-8")


def test_frontend_handles_inactive_membership_code_in_api_wrappers():
    html = _html()
    assert 'TENANT_MEMBERSHIP_INACTIVE_CODE = "tenant_membership_inactive"' in html
    assert 'headers?.get("X-Auth-Error-Code")' in html
    # financeApi·workspaceApi·authApi + 입사서류 파일 열기(2026-10-30, 401·소속 해제 처리 공유)
    assert len(re.findall(r"throw buildApiError\(response, payload,", html)) == 4
    body = re.search(r"function handleTenantMembershipInactive\(\) \{(.*?)\n    \}\n", html, re.S).group(1)
    assert "tenantMembershipInactiveHandled" in body
    assert "clearServerAuthToken()" in body and "saveAuthSession(null)" in body
    assert 'setAuthMode("login")' in body
    assert "회사 소속이 바뀌어 다시 로그인이 필요합니다" in html


def test_frontend_handler_runs_once_and_preserves_contract_link():
    html = _html()
    body = re.search(r"function handleTenantMembershipInactive\(\) \{(.*?)\n    \}\n", html, re.S).group(1)
    assert body.lstrip().startswith("if (tenantMembershipInactiveHandled) return;")
    assert "location" not in body and "yf_contract_token" not in body  # 주소를 건드리지 않아 서명 링크가 남는다
    persist = re.search(r"function persistServerAuthToken\(token\) \{(.*?)\n    \}\n", html, re.S).group(1)
    assert "tenantMembershipInactiveHandled = false" in persist
    assert 'id="authGateNotice"' in html
