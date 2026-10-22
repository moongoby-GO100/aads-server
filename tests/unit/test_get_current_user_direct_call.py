"""get_current_user 를 Depends 없이 직접 호출해도 Header 기본값 객체 때문에 죽지 않아야 한다."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret-key-that-is-long-enough")

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.auth as auth_module
from app.api import pipeline_runner


def _request(path: str = "/api/v1/pipeline/jobs") -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})


class _Conn:
    async def fetchrow(self, *_a, **_k):
        return None


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield _Conn()


def _patch_bearer(monkeypatch):
    monkeypatch.setattr(auth_module, "verify_token", lambda t: {"sub": "u1", "email": "a@b.c", "is_admin": True} if t == "tok" else None)

    async def fake_ctx(user, requested_tenant_id=None):
        assert requested_tenant_id is None
        return {
            "tenant": {"id": "t1", "kind": "internal"},
            "membership": {"role": "owner"},
            "user_role": "admin",
        }

    monkeypatch.setattr(auth_module, "_load_tenant_context", fake_ctx)


@pytest.mark.asyncio
async def test_direct_call_with_omitted_args_does_not_raise_attribute_error():
    with pytest.raises(HTTPException) as exc:
        await auth_module.get_current_user(_request())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_direct_call_bearer_path_works_with_omitted_headers(monkeypatch):
    _patch_bearer(monkeypatch)
    user = await auth_module.get_current_user(_request(), authorization="Bearer tok")
    assert user["user_id"] == "u1"
    assert user["tenant_role"] == "owner"


@pytest.mark.asyncio
async def test_pipeline_runner_dependency_bearer_path(monkeypatch):
    _patch_bearer(monkeypatch)

    async def no_internal(*_a, **_k):
        return None

    monkeypatch.setattr(pipeline_runner, "_internal_pipeline_context", no_internal)
    dep = pipeline_runner.require_tenant_viewer
    ctx = await dep(_request(), authorization="Bearer tok", x_tenant_id=None, x_monitor_key=None)
    assert ctx["membership"]["role"] == "owner"
    assert ctx["user"]["user_id"] == "u1"
