"""Agent Vault browser resolution must not substitute another account or origin."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.services import agent_vault_service as vault


TENANT = "00000000-0000-0000-0000-000000000002"


@pytest.mark.asyncio
async def test_resolve_requires_exact_tenant_origin_work_key_and_account(monkeypatch):
    rows = [{"id": "one", "work_key": "aads", "username_enc": "account-a"},
            {"id": "two", "work_key": "aads", "username_enc": "account-b"}]
    conn = AsyncMock()

    async def fetch(query, *args):
        assert "tenant_id = $1" in query
        assert "origin = $2" in query
        assert "work_key = $3" in query
        assert str(args[0]) == TENANT
        assert args[2] == "aads"
        return rows if args[1] == "https://store.coupangeats.com" else []

    conn.fetch.side_effect = fetch

    @asynccontextmanager
    async def acquire():
        yield conn

    monkeypatch.setattr(vault, "get_pool", lambda: type("Pool", (), {"acquire": staticmethod(acquire)})())
    monkeypatch.setattr(vault, "decrypt_value", lambda value: value)
    monkeypatch.setattr(vault, "_row_to_credential", lambda row, **kwargs: {"id": row["id"]})
    monkeypatch.setattr(vault, "write_access_log", AsyncMock())

    assert await vault.get_agent_credential_for_url(
        tenant_id=TENANT, url="https://store.coupangeats.com/login", work_key="aads", username="account-b"
    ) == {"id": "two"}
    assert await vault.get_agent_credential_for_url(
        tenant_id=TENANT, url="https://store.coupangeats.com/login", work_key="aads", username="account"
    ) is None
    assert await vault.get_agent_credential_for_url(
        tenant_id=TENANT, url="https://store.coupangeats.com/login", work_key="aads"
    ) is None
    assert await vault.get_agent_credential_for_url(
        tenant_id=TENANT, url="https://store.coupangeats.com.evil/login", work_key="aads", username="account-b"
    ) is None
    rows.pop(0)
    assert await vault.get_agent_credential_for_url(
        tenant_id=TENANT, url="https://store.coupangeats.com/login", work_key="aads"
    ) == {"id": "two"}
    assert await vault.get_agent_credential_for_url(
        tenant_id=TENANT, url="https://store.coupangeats.com/login", username="account-b"
    ) is None


def test_origin_metadata_maps_to_only_same_origin_login_url(monkeypatch):
    monkeypatch.setattr(vault, "decrypt_value", lambda value: "account")
    row = {
        "id": "one", "tenant_id": TENANT, "work_key": "aads",
        "origin": "https://store.coupangeats.com", "label": "account",
        "username_enc": None, "password_enc": None, "is_active": True,
        "metadata": {"login_url": "https://store.coupangeats.com/merchant/login"},
    }
    assert vault._row_to_credential(row)["login_url"] == "https://store.coupangeats.com/merchant/login"
    row["metadata"] = {"login_url": "https://store.coupangeats.com.evil/login"}
    assert vault._row_to_credential(row)["login_url"] == ""
