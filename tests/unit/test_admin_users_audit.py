from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "unit-test-secret")

from app.api import admin_users as admin_users_api
from app.auth import get_current_user


class _AuditConn:
    def __init__(
        self,
        *,
        active_missing: int,
        deleted_missing: int,
        chat_null_counts: dict[str, int],
    ):
        self.active_missing = active_missing
        self.deleted_missing = deleted_missing
        self.chat_null_counts = chat_null_counts

    async def fetchval(self, query: str, *args):
        if "FROM saas_users" in query and "deleted_at IS NULL" in query:
            return self.active_missing
        if "FROM saas_users" in query and "deleted_at IS NOT NULL" in query:
            return self.deleted_missing
        if "FROM chat_sessions" in query:
            return self.chat_null_counts.get("chat_sessions", 0)
        if "FROM chat_messages" in query:
            return self.chat_null_counts.get("chat_messages", 0)
        if "FROM chat_artifacts" in query:
            return self.chat_null_counts.get("chat_artifacts", 0)
        raise AssertionError(f"Unexpected query: {query}")


@pytest.mark.asyncio
async def test_tenant_isolation_audit_counts_active_missing_only_for_warning_total():
    conn = _AuditConn(
        active_missing=2,
        deleted_missing=5,
        chat_null_counts={
            "chat_sessions": 0,
            "chat_messages": 1,
            "chat_artifacts": 0,
        },
    )

    result = await admin_users_api._tenant_isolation_audit(
        conn,
        user_columns={"status", "deleted_at", "is_active"},
        tables={
            "chat_sessions": True,
            "chat_messages": True,
            "chat_artifacts": True,
        },
    )

    assert result["active_users_missing_default_tenant_id"] == 2
    assert result["deleted_users_missing_default_tenant_id"] == 5
    assert result["chat_sessions_tenant_id_null"] == 0
    assert result["chat_messages_tenant_id_null"] == 1
    assert result["chat_artifacts_tenant_id_null"] == 0
    assert result["warning_count"] == 3


def test_internal_admin_api_is_blocked_for_customer_tenant():
    app = FastAPI()
    app.include_router(admin_users_api.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "customer-1",
        "email": "customer@example.com",
        "tenant_id": "customer-tenant",
        "is_internal_admin": False,
    }

    response = TestClient(app).get("/api/v1/admin/users/overview")

    assert response.status_code == 403
    assert response.json()["detail"] == "Internal admin access required"
