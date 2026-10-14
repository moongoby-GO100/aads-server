"""Exact (tenant, origin, account) Vault matching and post-navigation injection gate."""
import inspect
import json
import uuid
from contextlib import asynccontextmanager

import pytest

from app.services import agent_vault_service as vault

TENANT_A = "00000000-0000-0000-0000-00000000000a"
TENANT_B = "00000000-0000-0000-0000-00000000000b"


def _row(origin, user, *, tenant=TENANT_A, work_key="wk", label="default", metadata=None):
    return {
        "id": uuid.uuid4(),
        "tenant_id": uuid.UUID(tenant),
        "work_key": work_key,
        "origin": origin,
        "label": label,
        "username_enc": f"enc:{user}",
        "password_enc": "enc:PLACEHOLDER",
        "metadata": metadata if metadata is not None else {},
        "is_active": True,
        "last_used_at": None,
        "created_at": None,
        "updated_at": None,
    }


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.logs = []

    async def fetch(self, query, *args):
        tenant = args[0]
        return [r for r in self.rows if r["tenant_id"] == tenant and r["is_active"]]

    async def fetchrow(self, query, *args):
        rows = [r for r in self.rows if r["tenant_id"] == args[0] and r["origin"] == args[1]]
        return rows[0] if rows else None

    async def execute(self, query, *args):
        self.logs.append(args)


@pytest.fixture
def conn(monkeypatch):
    holder = _Conn([])

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield holder

    monkeypatch.setattr(vault, "get_pool", lambda: _Pool())
    monkeypatch.setattr(vault, "decrypt_value", lambda v: str(v).removeprefix("enc:"))
    return holder


@pytest.mark.asyncio
async def test_exact_tenant_origin_account_matches(conn):
    conn.rows = [
        _row("https://store.coupangeats.com", "acct-1", label="매장A"),
        _row("https://store.coupangeats.com", "acct-2", label="매장B"),
    ]
    cred = await vault.match_credential_exact(
        tenant_id=TENANT_A, url="https://STORE.coupangeats.com/merchant/login?x=1", account="acct-2",
    )
    assert cred["label"] == "매장B"
    assert cred["username"] == "acct-2"
    assert cred["tenant_id"] == TENANT_A


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://store.coupangeats.com.evil/login",
        "https://store.coupangeats.com.evil.example",
        "https://evil.store.coupangeats.com/login",
        "https://coupang.com/login",
        "http://store.coupangeats.com/login",
        "https://store.coupangeats.com:8443/login",
        "https://store.coupangeats.com@evil.example/login",
        "https://evil.example@store.coupangeats.com/login",
        "store.coupangeats.com",
        "",
    ],
)
async def test_lookalike_or_inexact_origin_never_matches(conn, url):
    conn.rows = [_row("https://store.coupangeats.com", "acct-1")]
    assert await vault.match_credential_exact(tenant_id=TENANT_A, url=url, account="acct-1") is None


@pytest.mark.asyncio
async def test_coupang_does_not_match_coupangeats_in_either_direction(conn):
    conn.rows = [_row("https://login.coupang.com", "acct-1"), _row("https://store.coupangeats.com", "acct-1")]
    assert await vault.match_credential_exact(tenant_id=TENANT_A, url="https://coupang.com", account="acct-1") is None
    assert await vault.match_credential_exact(tenant_id=TENANT_A, url="https://coupangeats.com", account="acct-1") is None
    assert (await vault.match_credential_exact(tenant_id=TENANT_A, url="https://login.coupang.com/x", account="acct-1"))["origin"] == "https://login.coupang.com"


@pytest.mark.asyncio
async def test_other_tenant_never_matches(conn):
    conn.rows = [_row("https://store.coupangeats.com", "acct-1", tenant=TENANT_B)]
    assert await vault.match_credential_exact(tenant_id=TENANT_A, url="https://store.coupangeats.com", account="acct-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["acct-9", "ACCT-1", " acct-1", "", "   "])
async def test_other_or_blank_account_never_matches(conn, account):
    conn.rows = [_row("https://store.coupangeats.com", "acct-1")]
    assert await vault.match_credential_exact(tenant_id=TENANT_A, url="https://store.coupangeats.com", account=account) is None


@pytest.mark.asyncio
async def test_login_url_origin_counts_and_lookalike_of_it_does_not(conn):
    conn.rows = [_row("https://wing.coupang.com", "acct-1", metadata=json.dumps({"login_url": "https://xauth.coupang.com/auth/login"}))]
    hit = await vault.match_credential_exact(tenant_id=TENANT_A, url="https://xauth.coupang.com/x", account="acct-1")
    assert hit["origin"] == "https://wing.coupang.com"
    assert await vault.match_credential_exact(tenant_id=TENANT_A, url="https://xauth.coupang.com.evil/x", account="acct-1") is None
    assert await vault.match_credential_exact(tenant_id=TENANT_A, url="https://coupang.com/x", account="acct-1") is None


@pytest.mark.asyncio
async def test_metadata_is_returned_unmasked_for_login_steps_and_log_has_no_secret(conn):
    steps = [{"action": "fill", "selector": "#id"}, {"action": "click", "selector": "button"}]
    conn.rows = [_row("https://store.coupangeats.com", "acct-1", metadata={"login_steps": steps, "note": "keep"})]
    cred = await vault.match_credential_exact(tenant_id=TENANT_A, url="https://store.coupangeats.com", account="acct-1")
    assert cred["metadata"] == {"login_steps": steps, "note": "keep"}
    assert cred["password"] != "********"
    logged = json.dumps(conn.logs, default=str)
    assert "PLACEHOLDER" not in logged and "acct-1" not in logged
    assert "credential_exact_resolve" in logged


@pytest.mark.asyncio
async def test_work_key_prefers_but_does_not_require_match(conn):
    conn.rows = [_row("https://store.coupangeats.com", "acct-1", work_key="a"), _row("https://store.coupangeats.com", "acct-1", work_key="b", label="z")]
    pref = await vault.match_credential_exact(tenant_id=TENANT_A, url="https://store.coupangeats.com", account="acct-1", work_key="b")
    assert pref["work_key"] == "b"


def test_existing_lookup_signature_still_works_without_account():
    params = inspect.signature(vault.get_agent_credential_for_url).parameters
    assert list(params) == ["tenant_id", "url", "work_key", "user_id"]
    assert "account" not in params


@pytest.mark.asyncio
async def test_existing_lookup_unchanged_returns_secret_without_account(conn):
    conn.rows = [_row("https://store.coupangeats.com", "acct-1", metadata={"login_steps": [1]})]
    cred = await vault.get_agent_credential_for_url(tenant_id=TENANT_A, url="https://store.coupangeats.com/login")
    assert cred["username"] == "acct-1"
    assert cred["metadata"] == {"login_steps": [1]}
    assert set(cred) == {
        "id", "tenant_id", "work_key", "origin", "label", "username", "password",
        "metadata", "is_active", "last_used_at", "created_at", "updated_at",
    }


@pytest.mark.parametrize(
    "final_url,ok,reason",
    [
        ("https://store.coupangeats.com/merchant/login", True, "origin_confirmed"),
        ("https://store.coupangeats.com:443/x", True, "origin_confirmed"),
        ("https://evil.example/landing", False, "final_origin_mismatch"),
        ("https://store.coupangeats.com.evil/login", False, "final_origin_mismatch"),
        ("http://store.coupangeats.com/login", False, "final_origin_mismatch"),
        ("about:blank", False, "final_origin_unknown"),
        ("", False, "final_origin_unknown"),
    ],
)
def test_injection_requires_the_actual_final_origin(final_url, ok, reason):
    verdict = vault.verify_injection_origin(credential_origin="https://store.coupangeats.com", final_url=final_url)
    assert verdict["ok"] is ok
    assert verdict["reason"] == reason


def test_injection_accepts_login_url_origin_and_rejects_invalid_credential_origin():
    assert vault.verify_injection_origin(
        credential_origin="https://wing.coupang.com", login_url="https://xauth.coupang.com/login", final_url="https://xauth.coupang.com/a",
    )["ok"]
    assert vault.verify_injection_origin(credential_origin="", final_url="https://xauth.coupang.com")["reason"] == "credential_origin_invalid"
