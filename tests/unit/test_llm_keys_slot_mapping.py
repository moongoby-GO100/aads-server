import pytest
from datetime import datetime, timedelta, timezone
import hashlib

from app.api import llm_keys
from app.api.llm_keys import _anthropic_slot_map, _binding_account_mismatch


def test_anthropic_slot_map_uses_canonical_slot_not_priority_order():
    records = [
        {
            "key_name": "ANTHROPIC_AUTH_TOKEN_3",
            "slot": "3",
            "priority": 1,
        },
        {
            "key_name": "ANTHROPIC_AUTH_TOKEN_2",
            "slot": "2",
            "priority": 2,
        },
        {
            "key_name": "ANTHROPIC_AUTH_TOKEN",
            "slot": "1",
            "priority": 3,
        },
    ]

    assert _anthropic_slot_map(records) == {
        "ANTHROPIC_AUTH_TOKEN_3": "slot3",
        "ANTHROPIC_AUTH_TOKEN_2": "slot2",
        "ANTHROPIC_AUTH_TOKEN": "slot1",
    }


def test_anthropic_slot_map_ignores_unaddressable_records():
    assert _anthropic_slot_map(
        [
            {"key_name": "ANTHROPIC_AUTH_TOKEN_3", "slot": ""},
            {"key_name": "", "slot": "4"},
        ]
    ) == {}


def test_binding_account_mismatch_requires_full_label_email():
    binding = {"actual_account": "moong76@gmail.com"}

    assert _binding_account_mismatch("moongoby@naver.com", binding) is True
    assert _binding_account_mismatch("moong76@gmail.com", binding) is False
    assert _binding_account_mismatch("jinah-biseo(244)", binding) is False
    assert _binding_account_mismatch("moongoby@naver.com", {}) is False


@pytest.mark.asyncio
async def test_slot4_wrong_account_keeps_quarantine(monkeypatch):
    async def relay_call(method, path):
        assert (method, path) == ("GET", "/account-bindings")
        return {"bindings": [{"target": "claude:4", "bound": True,
                              "needs_login": False, "actual_account": "wrong@example.com"}]}

    monkeypatch.setattr(llm_keys, "_relay_call", relay_call)
    assert await llm_keys._reconcile_successful_account_login(
        "login-id", {"target": "claude:4", "credential_fingerprint": "f" * 64}
    ) is False


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Connection:
    def __init__(self, record):
        self.record = record
        self.executed = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, *_args):
        return self.record

    async def execute(self, query, *args):
        self.executed.append((query, args))


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["identity", "fingerprint", "expiry", "missing_db"])
async def test_slot4_failed_proof_never_activates(monkeypatch, failure):
    token = "sk-ant-oat01-test-token"
    conn = _Connection(None if failure == "missing_db" else {
        "key_name": "ANTHROPIC_AUTH_TOKEN_4", "label": "slot4",
        "encrypted_value": "cipher", "oauth_expires_at": (
            datetime.now(timezone.utc) + timedelta(minutes=2)
            if failure == "expiry" else datetime.now(timezone.utc) + timedelta(hours=1)
        ),
    })
    monkeypatch.setattr(llm_keys, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(llm_keys, "decrypt_value", lambda _value: token)
    monkeypatch.setattr(llm_keys, "invalidate_key_cache", lambda *_: None)
    monkeypatch.setattr(llm_keys, "invalidate_registry_cache", lambda: None)

    async def relay_call(_method, _path):
        account = "wrong@example.com" if failure == "identity" else "thelylon14@gmail.com"
        return {"bindings": [{"target": "claude:4", "bound": True,
                              "needs_login": False, "actual_account": account}]}

    monkeypatch.setattr(llm_keys, "_relay_call", relay_call)
    fingerprint = "0" * 64 if failure == "fingerprint" else hashlib.sha256(token.encode()).hexdigest()
    assert await llm_keys._reconcile_successful_account_login(
        "login-id", {"target": "claude:4", "credential_fingerprint": fingerprint}
    ) is False
    assert conn.executed == []


@pytest.mark.asyncio
async def test_slot4_verified_proof_activates_and_records_snapshot(monkeypatch):
    token = "sk-ant-oat01-test-token"
    conn = _Connection({
        "key_name": "ANTHROPIC_AUTH_TOKEN_4", "label": "slot4", "encrypted_value": "cipher",
        "oauth_expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    })
    monkeypatch.setattr(llm_keys, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(llm_keys, "decrypt_value", lambda _value: token)
    monkeypatch.setattr(llm_keys, "invalidate_key_cache", lambda *_: None)
    monkeypatch.setattr(llm_keys, "invalidate_registry_cache", lambda: None)

    async def relay_call(_method, _path):
        return {"bindings": [{"target": "claude:4", "bound": True,
                              "needs_login": False, "actual_account": "thelylon14@gmail.com"}]}

    monkeypatch.setattr(llm_keys, "_relay_call", relay_call)
    assert await llm_keys._reconcile_successful_account_login(
        "login-id", {"target": "claude:4",
                     "credential_fingerprint": hashlib.sha256(token.encode()).hexdigest()}
    ) is True
    assert len(conn.executed) == 2
    assert "is_active = TRUE" in conn.executed[0][0]
    assert "claude_max_usage_snapshot" in conn.executed[1][0]
