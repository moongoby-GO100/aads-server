"""ACCT-SIGNUP-S1: 약관동의 이력 저장 + last_login_at 갱신.

DB 접속 없이 asyncpg pool/conn 을 페이크로 대체해 검증한다.
consents 는 선택 필드다 — 미전송 시 기존 회원가입 동작이 그대로 성공해야 한다.
"""
import os

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.auth as auth_module
from app.api import auth as auth_router


class _AsyncTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _AsyncAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AsyncAcquire(self.conn)


class FakeConn:
    def __init__(self, fetchrows=None):
        self.fetchrows = list(fetchrows or [])
        self.execute_calls = []

    def transaction(self):
        return _AsyncTransaction()

    async def fetchrow(self, query, *args):
        return self.fetchrows.pop(0) if self.fetchrows else None

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "OK"


def _build_app():
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/v1")
    return app


def test_register_without_consents_keeps_existing_behavior(monkeypatch):
    """consents 미전송 시 회원가입은 기존과 동일하게 성공한다 (회귀 방지)."""
    calls = {}

    async def fake_require_saas_schema_ready():
        return None

    async def fake_get_saas_user_by_email(email):
        return None

    async def fake_create_saas_user(email, password, name, *, attach_internal_tenant, consents, ip, user_agent):
        calls["consents"] = consents
        return {"id": "u1", "email": email, "name": name, "default_tenant_id": None, "created_at": None}

    async def fake_create_tenant_for_user(*, user_id, name, plan_key):
        return {"tenant_id": "t1", "name": name}

    monkeypatch.setattr(auth_module, "require_saas_schema_ready", fake_require_saas_schema_ready)
    monkeypatch.setattr(auth_module, "get_saas_user_by_email", fake_get_saas_user_by_email)
    monkeypatch.setattr(auth_module, "create_saas_user", fake_create_saas_user)
    monkeypatch.setattr(auth_module, "create_tenant_for_user", fake_create_tenant_for_user)
    monkeypatch.setattr(auth_module, "create_token", lambda *a, **kw: "tok")

    client = TestClient(_build_app())
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "a@example.com", "password": "abcdef", "name": "A"},
    )

    assert resp.status_code == 200, resp.text
    assert calls["consents"] is None


def test_register_required_consent_false_returns_400(monkeypatch):
    called = {"create_saas_user": False}

    async def fake_create_saas_user(*args, **kwargs):
        called["create_saas_user"] = True
        return None

    monkeypatch.setattr(auth_module, "create_saas_user", fake_create_saas_user)

    client = TestClient(_build_app())
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "b@example.com",
            "password": "abcdef",
            "consents": [
                {"consent_key": "terms", "version": "v1", "agreed": False},
                {"consent_key": "privacy", "version": "v1", "agreed": True},
            ],
        },
    )

    assert resp.status_code == 400
    assert called["create_saas_user"] is False


def test_register_optional_marketing_consent_false_is_allowed(monkeypatch):
    async def fake_require_saas_schema_ready():
        return None

    async def fake_get_saas_user_by_email(email):
        return None

    async def fake_create_saas_user(email, password, name, *, attach_internal_tenant, consents, ip, user_agent):
        return {"id": "u2", "email": email, "name": name, "default_tenant_id": None, "created_at": None}

    async def fake_create_tenant_for_user(*, user_id, name, plan_key):
        return {"tenant_id": "t2", "name": name}

    monkeypatch.setattr(auth_module, "require_saas_schema_ready", fake_require_saas_schema_ready)
    monkeypatch.setattr(auth_module, "get_saas_user_by_email", fake_get_saas_user_by_email)
    monkeypatch.setattr(auth_module, "create_saas_user", fake_create_saas_user)
    monkeypatch.setattr(auth_module, "create_tenant_for_user", fake_create_tenant_for_user)
    monkeypatch.setattr(auth_module, "create_token", lambda *a, **kw: "tok")

    client = TestClient(_build_app())
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "c@example.com",
            "password": "abcdef",
            "consents": [
                {"consent_key": "terms", "version": "v1", "agreed": True},
                {"consent_key": "privacy", "version": "v1", "agreed": True},
                {"consent_key": "age14", "version": "v1", "agreed": True},
                {"consent_key": "marketing", "version": "v1", "agreed": False},
            ],
        },
    )

    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_create_saas_user_inserts_consents_in_same_transaction(monkeypatch):
    fake_user_row = {
        "id": "u3",
        "email": "d@example.com",
        "name": "D",
        "default_tenant_id": None,
        "created_at": None,
    }
    conn = FakeConn(fetchrows=[fake_user_row])
    pool = FakePool(conn)

    async def fake_ensure_pool():
        return pool

    monkeypatch.setattr(auth_module, "_ensure_pool", fake_ensure_pool)
    monkeypatch.setattr(auth_module, "BCRYPT_AVAILABLE", True)

    class _FakeBcrypt:
        @staticmethod
        def hashpw(pw, salt):
            return b"hashed"

        @staticmethod
        def gensalt():
            return b"salt"

    monkeypatch.setattr(auth_module, "_bcrypt_mod", _FakeBcrypt)

    result = await auth_module.create_saas_user(
        "d@example.com",
        "abcdef",
        "D",
        consents=[{"consent_key": "terms", "version": "v1", "agreed": True}],
        ip="127.0.0.1",
        user_agent="pytest",
    )

    assert result is not None
    insert_queries = [q for q, _ in conn.execute_calls if "saas_user_consents" in q]
    assert len(insert_queries) == 1
    query, args = conn.execute_calls[0]
    assert "INSERT INTO saas_user_consents" in query
    assert args[0] == "u3"
    assert args[1] == "terms"
    assert args[3] is True


@pytest.mark.asyncio
async def test_update_saas_user_last_login_runs_update(monkeypatch):
    conn = FakeConn()
    pool = FakePool(conn)

    async def fake_ensure_pool():
        return pool

    monkeypatch.setattr(auth_module, "_ensure_pool", fake_ensure_pool)

    await auth_module.update_saas_user_last_login("u3")

    assert len(conn.execute_calls) == 1
    query, args = conn.execute_calls[0]
    assert "UPDATE saas_users" in query
    assert "last_login_at" in query
    assert args == ("u3",)


@pytest.mark.asyncio
async def test_update_saas_user_last_login_swallows_errors(monkeypatch):
    async def fake_ensure_pool():
        raise RuntimeError("db down")

    monkeypatch.setattr(auth_module, "_ensure_pool", fake_ensure_pool)

    # 예외가 로그인 흐름을 막지 않도록 삼켜야 한다.
    await auth_module.update_saas_user_last_login("u3")


def test_login_success_calls_update_last_login(monkeypatch):
    recorded = {}

    async def fake_authenticate_saas_user(email, password):
        return {"id": "u4", "email": email, "name": "D", "tenant_id": None, "role": "user"}

    async def fake_resolve_login_tenant_for_user(user):
        return "t4"

    async def fake_update_last_login(user_id):
        recorded["user_id"] = user_id

    monkeypatch.setattr(auth_module, "JWT_AVAILABLE", True)
    monkeypatch.setattr(auth_module, "require_saas_schema_ready", lambda: _immediate(None))
    monkeypatch.setattr(auth_module, "authenticate_saas_user", fake_authenticate_saas_user)
    monkeypatch.setattr(auth_module, "resolve_login_tenant_for_user", fake_resolve_login_tenant_for_user)
    monkeypatch.setattr(auth_module, "update_saas_user_last_login", fake_update_last_login)
    monkeypatch.setattr(auth_module, "create_token", lambda *a, **kw: "tok")

    client = TestClient(_build_app())
    resp = client.post("/api/v1/auth/login", json={"email": "d@example.com", "password": "abcdef"})

    assert resp.status_code == 200, resp.text
    assert recorded["user_id"] == "u4"


async def _immediate(value):
    return value


def test_register_partial_required_consents_returns_400(monkeypatch):
    """필수 3종 중 일부만 보내면 거절한다 — 누락된 항목은 미동의로 본다."""
    called = {"create_saas_user": False}

    async def fake_create_saas_user(*args, **kwargs):
        called["create_saas_user"] = True
        return None

    monkeypatch.setattr(auth_module, "create_saas_user", fake_create_saas_user)

    client = TestClient(_build_app())
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "d@example.com",
            "password": "abcdef",
            "consents": [
                {"consent_key": "terms", "version": "v1", "agreed": True},
            ],
        },
    )

    assert resp.status_code == 400
    assert "privacy" in resp.text
    assert "age14" in resp.text
    assert called["create_saas_user"] is False
