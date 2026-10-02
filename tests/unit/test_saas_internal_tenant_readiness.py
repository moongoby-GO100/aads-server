"""require_saas_schema_ready 가 slug 가 아닌 aads_internal_tenant_id() 반환 tenant 를 검증하는지."""
import os
import uuid

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")
os.environ.setdefault("E2B_API_KEY", "unit-test-e2b-key")

import app.auth as auth_module
from app.api import auth as auth_api

ALL_READY = {
    "has_saas_users": True,
    "has_tenants": True,
    "has_tenant_memberships": True,
    "has_tenant_invites": True,
    "has_internal_tenant_fn": True,
    "has_saas_user_columns": True,
    "has_membership_columns": True,
}


class FakeConn:
    """tenants 테이블과 aads_internal_tenant_id() 를 흉내낸다. SQL 이 무엇을 보는지에 따라 답이 달라진다."""

    def __init__(self, tenants, fn_tenant_id, fn_exists=True):
        self.tenants = tenants  # {id: {"slug": str, "deleted": bool}}
        self.fn_tenant_id = fn_tenant_id
        self.row = dict(ALL_READY, has_internal_tenant_fn=fn_exists)

    async def fetchrow(self, sql, *args):
        if "FROM saas_users" in sql:
            return None
        return self.row

    async def fetchval(self, sql, *args):
        if "slug = 'internal'" in sql:
            return any(t["slug"] == "internal" and not t["deleted"] for t in self.tenants.values())
        if "aads_internal_tenant_id()" in sql:
            t = self.tenants.get(self.fn_tenant_id)
            return bool(t) and not t["deleted"]
        raise AssertionError(f"unexpected SQL: {sql}")


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest.fixture()
def install(monkeypatch):
    def _install(conn):
        monkeypatch.setattr(auth_module, "_pool", FakePool(conn))
        monkeypatch.setattr(auth_module, "_saas_schema_ready", False)

    return _install


async def test_resolves_explicit_tenant_without_internal_slug(install):
    tid = uuid.uuid4()
    install(FakeConn({tid: {"slug": "obys-main", "deleted": False}}, tid))
    await auth_module.require_saas_schema_ready()
    assert auth_module._saas_schema_ready is True


async def test_resolves_legacy_internal_slug(install):
    tid = uuid.uuid4()
    install(FakeConn({tid: {"slug": "internal", "deleted": False}}, tid))
    await auth_module.require_saas_schema_ready()
    assert auth_module._saas_schema_ready is True


async def test_function_returns_nonexistent_tenant_is_503(install):
    other = uuid.uuid4()
    install(FakeConn({other: {"slug": "obys-main", "deleted": False}}, uuid.uuid4()))
    with pytest.raises(HTTPException) as exc:
        await auth_module.require_saas_schema_ready()
    assert exc.value.status_code == 503
    assert exc.value.detail == "Internal tenant is not initialized"
    assert auth_module._saas_schema_ready is False


async def test_function_returns_null_is_503(install):
    tid = uuid.uuid4()
    install(FakeConn({tid: {"slug": "obys-main", "deleted": False}}, None))
    with pytest.raises(HTTPException) as exc:
        await auth_module.require_saas_schema_ready()
    assert exc.value.status_code == 503


async def test_deleted_tenant_is_503_even_with_internal_slug(install):
    tid = uuid.uuid4()
    install(FakeConn({tid: {"slug": "internal", "deleted": True}}, tid))
    with pytest.raises(HTTPException) as exc:
        await auth_module.require_saas_schema_ready()
    assert exc.value.status_code == 503
    assert auth_module._saas_schema_ready is False


async def test_missing_function_is_503_schema_not_initialized(install):
    tid = uuid.uuid4()
    install(FakeConn({tid: {"slug": "internal", "deleted": False}}, tid, fn_exists=False))
    with pytest.raises(HTTPException) as exc:
        await auth_module.require_saas_schema_ready()
    assert exc.value.status_code == 503
    assert exc.value.detail == "SaaS schema is not initialized"


async def test_login_bad_credentials_is_401_when_ready(install, monkeypatch):
    tid = uuid.uuid4()
    install(FakeConn({tid: {"slug": "obys-main", "deleted": False}}, tid))
    monkeypatch.setattr(auth_module, "JWT_AVAILABLE", True)
    monkeypatch.setattr(auth_module, "ADMIN_PASSWORD", "")
    with pytest.raises(HTTPException) as exc:
        await auth_api.login(auth_api.LoginRequest(email="nobody@example.invalid", password="wrong-password"))
    assert exc.value.status_code == 401


async def test_login_is_503_not_401_when_tenant_not_ready(install, monkeypatch):
    other = uuid.uuid4()
    install(FakeConn({other: {"slug": "obys-main", "deleted": False}}, uuid.uuid4()))
    monkeypatch.setattr(auth_module, "JWT_AVAILABLE", True)
    monkeypatch.setattr(auth_module, "ADMIN_PASSWORD", "")
    with pytest.raises(HTTPException) as exc:
        await auth_api.login(auth_api.LoginRequest(email="nobody@example.invalid", password="wrong-password"))
    assert exc.value.status_code == 503
