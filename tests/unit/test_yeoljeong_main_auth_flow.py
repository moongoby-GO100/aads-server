"""오비서 전용 앱(app.yeoljeong_main) 의 로그인·타조직 거부 회귀.

후보 컨테이너 검증에서 "잘못된 로그인 401" 만 확인되고 정상 로그인 경로는 증거가
없었다. 실제 계정 없이도 전용 앱의 미들웨어+라우터 조합이 토큰 발급 → 인증 통과 →
타조직 403 으로 이어지는지 DB 없이 고정한다.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.auth as auth_module
from app import yeoljeong_main

_USER = {"id": 7, "email": "owner@example.test", "name": "Owner"}
_TENANT_A = "11111111-1111-1111-1111-111111111111"
_TENANT_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth_module, "SECRET_KEY", "test-secret-key-for-yeoljeong-auth-flow-0123456789")
    monkeypatch.setattr(auth_module, "ADMIN_PASSWORD", "")

    async def _ready():
        return None

    async def _authenticate(email, password):
        if email == _USER["email"] and password == "correct-pw":
            return dict(_USER)
        return None

    async def _resolve_tenant(user):
        return _TENANT_A

    async def _touch(user_id):
        return None

    async def _load_context(user, requested_tenant_id=None):
        tenant_id = requested_tenant_id or user.get("tenant_id")
        if tenant_id != _TENANT_A:
            raise HTTPException(status_code=403, detail="Tenant membership required")
        return {
            "tenant": {"id": _TENANT_A, "slug": "a", "name": "A", "kind": "customer", "status": "active"},
            "membership": {"id": "m1", "tenant_id": _TENANT_A, "user_id": user["user_id"], "role": "owner", "status": "active"},
            "user_role": "user",
        }

    monkeypatch.setattr(auth_module, "require_saas_schema_ready", _ready)
    monkeypatch.setattr(auth_module, "authenticate_saas_user", _authenticate)
    monkeypatch.setattr(auth_module, "resolve_login_tenant_for_user", _resolve_tenant)
    monkeypatch.setattr(auth_module, "update_saas_user_last_login", _touch)
    monkeypatch.setattr(auth_module, "_load_tenant_context", _load_context)
    return TestClient(yeoljeong_main.app, follow_redirects=False)


def _login(client, password="correct-pw"):
    return client.post("/api/v1/auth/login", json={"email": _USER["email"], "password": password})


def test_wrong_password_is_401_without_token(client):
    r = _login(client, "wrong-pw")
    assert r.status_code == 401
    assert "token" not in r.json()


def test_valid_login_issues_token_bound_to_tenant(client):
    r = _login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == _USER["email"]
    assert body["tenant_id"] == _TENANT_A
    payload = auth_module.verify_token(body["token"])
    assert payload and payload["sub"] == "7" and payload["tenant_id"] == _TENANT_A


def test_issued_token_passes_dedicated_app_gate(client):
    token = _login(client).json()["token"]
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["tenant_id"] == _TENANT_A
    assert client.get("/api/v1/auth/tenants").status_code == 401


def test_other_tenant_is_denied_with_valid_token(client):
    token = _login(client).json()["token"]
    r = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}", "X-Tenant-ID": _TENANT_B},
    )
    assert r.status_code == 403


def test_forged_token_is_rejected(client):
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401
