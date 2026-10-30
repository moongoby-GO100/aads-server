import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

import app.services.model_registry as registry
from app.api import llm_keys
from scripts import account_login

SECRET = "ya29.SUPER-SECRET-TOKEN-VALUE"


def _row(**kw):
    base = {"id": 1, "provider": "anthropic", "key_name": "ANTHROPIC_AUTH_TOKEN",
            "encrypted_value": "x", "label": "a", "priority": 1, "is_active": True,
            "rate_limited_until": None, "notes": "", "manual_disabled": False,
            "manual_disabled_at": None, "used_percent": None, "resets_at": None,
            "snapshot_at": None, "ok_72h": 0, "limit_72h": 0, "collected_at": None}
    base.update(kw)
    return base


def _run_overview(monkeypatch, relay_payload, rows=None):
    rows = rows if rows is not None else [
        _row(),
        _row(id=2, provider="codex", key_name="CODEX_OAUTH_A", priority=1),
    ]

    class Conn:
        async def fetch(self, *_a):
            return rows

    class Pool:
        def acquire(self):
            class A:
                async def __aenter__(s):
                    return Conn()

                async def __aexit__(s, *_):
                    return False

            return A()

    async def relay(*_a, **_k):
        if relay_payload is None:
            raise HTTPException(status_code=503, detail="relay down")
        return relay_payload

    async def no_clear():
        return {}

    async def no_rows(*args, **kwargs):
        return []

    from app.services import oauth_usage_tracker, account_primary
    monkeypatch.setattr(account_primary, "relay_slot_subscriptions", no_clear)
    from app.core import auth_provider
    monkeypatch.setattr(oauth_usage_tracker, "get_slot_usage_all", no_rows)
    monkeypatch.setattr(auth_provider, "get_oauth_key_records_async", no_rows)
    monkeypatch.setattr(llm_keys, "get_pool", lambda: Pool())
    monkeypatch.setattr(llm_keys, "_relay_call", relay)
    monkeypatch.setattr(llm_keys, "decrypt_value", lambda _v: "sk-ant-oat01-abcdef")
    monkeypatch.setattr(registry, "clear_expired_rate_limits", no_clear)
    return asyncio.run(llm_keys.llm_overview())


def _agy(overview):
    return [a for a in overview["accounts"] if a["provider"] == "antigravity"]


def test_bound_binding_gives_ok(monkeypatch):
    payload = {"bindings": [{"target": "antigravity:host", "kind": "antigravity",
                             "account": "me@example.com", "bound": True, "needs_login": False}]}
    ov = _run_overview(monkeypatch, payload)
    (acct,) = _agy(ov)
    assert acct["state"] == "ok" and acct["bound"] is True
    assert acct["key_name"] == "ANTIGRAVITY_HOST"
    assert acct["kind"] == "subscription"
    assert acct["label"] == "Google 구독 (Antigravity) · me@example.com"
    assert acct["login_target"] is None and acct["windows"] == []
    assert acct["toggleable"] is False and "id" not in acct


def test_needs_login_binding(monkeypatch):
    payload = {"bindings": [{"target": "antigravity:host", "kind": "antigravity",
                             "account": None, "bound": False, "needs_login": True}]}
    ov = _run_overview(monkeypatch, payload)
    (acct,) = _agy(ov)
    assert acct["state"] == "needs_login" and acct["needs_login"] is True
    assert acct["label"] == "Google 구독 (Antigravity)"
    assert ov["summary"]["action_required"] == 1


@pytest.mark.parametrize("payload", [None, {"bindings": []}])
def test_relay_silent_or_old_relay_is_unknown_but_listed(monkeypatch, payload):
    ov = _run_overview(monkeypatch, payload)
    (acct,) = _agy(ov)
    assert acct["state"] == "unknown"
    assert acct["bound"] is False and acct["needs_login"] is False


def test_summary_counts(monkeypatch):
    payload = {"bindings": [{"target": "antigravity:host", "kind": "antigravity",
                             "account": None, "bound": True, "needs_login": False}]}
    ov = _run_overview(monkeypatch, payload)
    assert ov["summary"]["antigravity"] == {"usable": 1, "total": 1}
    ov = _run_overview(monkeypatch, None)
    assert ov["summary"]["antigravity"] == {"usable": 0, "total": 1}


def test_existing_accounts_unchanged(monkeypatch):
    payload = {"bindings": [
        {"target": "codex:CODEX_OAUTH_A", "kind": "codex", "account": "CODEX_OAUTH_A",
         "bound": True, "needs_login": False},
        {"target": "antigravity:host", "kind": "antigravity", "account": None,
         "bound": True, "needs_login": False},
    ]}
    ov = _run_overview(monkeypatch, payload)
    others = [a for a in ov["accounts"] if a["provider"] != "antigravity"]
    assert sorted(a["key_name"] for a in others) == ["ANTHROPIC_AUTH_TOKEN", "CODEX_OAUTH_A"]
    codex = next(a for a in others if a["provider"] == "codex")
    assert codex["state"] == "ok" and codex["login_target"] == "codex:CODEX_OAUTH_A"
    assert "toggleable" not in codex
    assert ov["summary"]["total"] == 2
    assert ov["summary"]["subscription_total"] == 3
    assert {"codex", "anthropic", "apikey_total", "bindings_available"} <= set(ov["summary"])


def test_db_row_for_antigravity_is_not_duplicated(monkeypatch):
    rows = [_row(id=9, provider="antigravity", key_name="AGY_X")]
    ov = _run_overview(monkeypatch, {"bindings": []}, rows=rows)
    assert [a["key_name"] for a in _agy(ov)] == ["AGY_X"]


def test_antigravity_not_offered_as_key_target():
    assert "antigravity" in llm_keys.SUBSCRIPTION_PROVIDERS
    assert "antigravity" not in llm_keys.CANONICAL_PROVIDERS
    assert llm_keys.normalize_provider("antigravity") == "antigravity"


def _patch_agy(monkeypatch, tmp_path, with_bin=True):
    root = tmp_path / "gemini"
    monkeypatch.setattr(account_login, "ANTIGRAVITY_HOME_ROOT", root)
    binary = tmp_path / "agy"
    if with_bin:
        binary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(account_login, "ANTIGRAVITY_BIN", binary)
    monkeypatch.setattr(account_login, "CLAUDE_SLOT_ROOT", tmp_path / "slots")
    monkeypatch.setattr(account_login, "CODEX_ACCOUNTS_ROOT", tmp_path / "codex")
    return root


def test_bindings_uses_agy_identity_and_never_leaks_values(monkeypatch, tmp_path):
    root = _patch_agy(monkeypatch, tmp_path)
    (root / "antigravity-cli").mkdir(parents=True)
    token = root / "antigravity-cli" / "antigravity-oauth-token"
    token.write_text(json.dumps({"id_token_email": "agy@example.com", "token": {"access_token": SECRET, "expiry": "2099-01-01T00:00:00Z"}}))
    (root / "google_accounts.json").write_text(json.dumps({"active": "me@example.com", "old": []}))

    opened = []
    real_read_text = type(token).read_text

    def spy(self, *a, **k):
        opened.append(self.name)
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(type(token), "read_text", spy)
    try:
        result = account_login.bindings()
    finally:
        token.chmod(0o600)

    (agy,) = [b for b in result if b["kind"] == "antigravity"]
    assert agy["target"] == "antigravity:host"
    assert agy["bound"] is True and agy["needs_login"] is False
    assert agy["credential_present"] is True
    assert agy["account_verified"] is False
    assert agy["account"] == "agy@example.com"
    assert "google_accounts.json" not in opened
    assert SECRET not in json.dumps(result)


def test_bindings_unbound_when_token_or_binary_missing(monkeypatch, tmp_path):
    root = _patch_agy(monkeypatch, tmp_path)
    (agy,) = account_login.bindings()
    assert agy["bound"] is False and agy["needs_login"] is True and agy["account"] is None

    (root / "antigravity-cli").mkdir(parents=True)
    (root / "antigravity-cli" / "antigravity-oauth-token").write_text("{}")
    monkeypatch.setattr(account_login, "ANTIGRAVITY_BIN", tmp_path / "missing-agy")
    (agy,) = account_login.bindings()
    assert agy["bound"] is False and agy["needs_login"] is True


@pytest.mark.parametrize("binding,expected", [
    ({"bound": False, "needs_login": False}, "unknown"),
    ({"bound": False, "needs_login": True}, "needs_login"),
    ({"bound": True, "needs_login": False}, "ok"),
])
def test_antigravity_states_and_unknown_quota(binding, expected):
    account = llm_keys._antigravity_account({"antigravity:host": binding})
    assert account["state"] == expected
    assert account["used_percent"] is None
    assert account["usage_status"] == "unsupported"
    assert account["usage_summary"] is None
    assert account["ok_72h"] is None


def test_antigravity_rejects_unattributed_quota():
    binding = {"bound": True, "account": "me@example.com", "quota_account": "other@example.com",
               "windows": [{"used_percent": 35}], "used_percent": 35, "usage_status": "available"}
    account = llm_keys._antigravity_account({"antigravity:host": binding})
    assert account["windows"] == []
    assert account["used_percent"] is None
    assert account["usage_collected_at"] is None
    assert account["usage_status"] == "collection_failed"


def test_antigravity_relay_unavailable_contract():
    account = llm_keys._antigravity_account(None)
    assert account["usage_status"] == "collection_failed"
    assert account["actual_account"] is None


def test_antigravity_accepts_attributed_quota_and_allowlists_fields():
    binding = {"bound": True, "account": "me@example.com", "quota_account": "ME@example.com",
        "usage_status": "available", "usage_collected_at": "2026-10-30T10:00:00Z",
        "windows": [{"window_minutes": 300, "used_percent": 35,
                     "resets_at": "2026-10-30T15:00:00Z", "access_token": SECRET}]}
    account = llm_keys._antigravity_account({"antigravity:host": binding})
    assert account["used_percent"] == 35
    assert account["usage_status"] == "available"
    assert account["usage_collected_at"]
    assert SECRET not in json.dumps(account)
    binding["quota_account"] = "other@example.com"
    rejected = llm_keys._antigravity_account({"antigravity:host": binding})
    assert rejected["used_percent"] is None
    assert rejected["usage_status"] == "collection_failed"


@pytest.mark.parametrize("credential", [{}, {"id_token_email": "invalid"},
    {"id_token_email": "agy@example.com", "token": {"access_token": SECRET, "expiry": "2000-01-01T00:00:00Z"}}])
def test_antigravity_invalid_or_expired_credential_is_unknown(monkeypatch, tmp_path, credential):
    root = _patch_agy(monkeypatch, tmp_path)
    (root / "antigravity-cli").mkdir(parents=True)
    (root / "antigravity-cli" / "antigravity-oauth-token").write_text(json.dumps(credential))
    binding = account_login._antigravity_binding()
    assert binding["bound"] is False
    assert binding["needs_login"] is False
    assert llm_keys._antigravity_account({"antigravity:host": binding})["state"] == "unknown"


def test_antigravity_unreadable_credential_is_unknown(monkeypatch, tmp_path):
    root = _patch_agy(monkeypatch, tmp_path)
    (root / "antigravity-cli").mkdir(parents=True)
    path = root / "antigravity-cli" / "antigravity-oauth-token"
    path.write_text("{}")
    def denied(*args, **kwargs):
        raise PermissionError("unreadable")
    monkeypatch.setattr(type(path), "read_text", denied)
    binding = account_login._antigravity_binding()
    assert binding["bound"] is False
    assert binding["account"] is None
    assert binding["needs_login"] is False


def test_host_identity_reaches_overview(monkeypatch, tmp_path):
    root = _patch_agy(monkeypatch, tmp_path)
    (root / "antigravity-cli").mkdir(parents=True)
    (root / "antigravity-cli" / "antigravity-oauth-token").write_text(json.dumps({
        "id_token_email": "agy@example.com", "token": {"access_token": SECRET, "expiry": "2099-01-01T00:00:00Z"}}))
    binding = account_login._antigravity_binding()
    account, = _agy(_run_overview(monkeypatch, {"bindings": [binding]}))
    assert account["state"] == "ok"
    assert account["actual_account"] == "agy@example.com"
    assert account["account_source"] == "agy_credential_metadata"
    assert account["account_verified"] is False
    assert account["usage_status"] == "unsupported"
    assert account["used_percent"] is None


@pytest.mark.parametrize("percent", [-1, 101, float("nan"), True, "35"])
def test_quota_invalid_percent_is_not_presented_as_usage(percent):
    binding = {"bound": True, "account": "me@example.com", "quota_account": "me@example.com",
        "usage_status": "available", "usage_collected_at": "2026-10-30T10:00:00Z",
        "windows": [{"window_minutes": 300, "used_percent": percent, "resets_at": "2026-10-30T15:00:00Z"}]}
    account = llm_keys._antigravity_account({"antigravity:host": binding})
    assert account["usage_status"] == "collection_failed"
    assert account["windows"] == []
    assert account["used_percent"] is None
