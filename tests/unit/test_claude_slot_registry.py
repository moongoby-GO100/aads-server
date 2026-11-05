"""Claude 구독 슬롯 5(obys.aads@gmail.com) 등록과 슬롯 목록 단일 정의.

슬롯 3·4 때 이름 매핑이 빠져 레코드가 slot="" 로 떨어졌고 폴백에서 조용히
사라졌다. 이 시험은 슬롯 목록이 한 곳(app/core/claude_slots.py)에서만 정해지고
하드코딩 사본들이 거기서 어긋나지 않는지 지킨다.
"""
import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.api import llm_keys
from app.core import claude_slots
from app.core.auth_provider import LAST_RESORT_SLOTS, _KEY_NAME_SLOTS, _assign_slots
from app.services import model_selector
from scripts import account_login

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "sk-ant-oat01-test-token"


def test_slot5_name_mapping_gives_real_slot_not_blank():
    assert _KEY_NAME_SLOTS["ANTHROPIC_AUTH_TOKEN_5"] == "5"
    assert _KEY_NAME_SLOTS["env_oauth_5"] == "5"
    out = _assign_slots([
        {"key_name": "ANTHROPIC_AUTH_TOKEN", "value": "t1", "priority": 1},
        {"key_name": "ANTHROPIC_AUTH_TOKEN_5", "value": "t5", "priority": 5},
    ])
    assert {r["key_name"]: r["slot"] for r in out}["ANTHROPIC_AUTH_TOKEN_5"] == "5"


def test_every_registered_slot_is_known_and_slot5_is_not_last_resort():
    assert "5" in model_selector._KNOWN_SLOTS
    assert tuple(model_selector._KNOWN_SLOTS) == claude_slots.CLAUDE_SLOTS
    assert "5" not in LAST_RESORT_SLOTS
    for name, slot in _KEY_NAME_SLOTS.items():
        assert slot in claude_slots.CLAUDE_SLOTS, name


def test_slot5_is_in_chat_candidates_without_gate(monkeypatch):
    async def _fetch(include_rate_limited=True):
        return [
            {"key_name": "ANTHROPIC_AUTH_TOKEN", "slot": "1", "priority": 1},
            {"key_name": "ANTHROPIC_AUTH_TOKEN_5", "slot": "5", "priority": 5},
        ]

    async def _never_gate(slot):
        raise AssertionError("일반 후보는 최후 수단 스위치를 묻지 않는다")

    monkeypatch.setattr(model_selector, "_ap_get_key_records_async", _fetch)
    monkeypatch.setattr(model_selector, "_slot_gate_enabled", _never_gate)
    slots = asyncio.run(model_selector._get_claude_slot_records())
    assert set(slots) == {"1", "5"}


def test_resolve_target_accepts_registered_slot_and_rejects_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(account_login, "CLAUDE_SLOT_ROOT", tmp_path)
    plan = account_login.resolve_target("claude:5")
    assert plan["home"] == tmp_path / "slot5"
    assert plan["credential"] == tmp_path / "slot5" / ".claude" / ".credentials.json"
    for bad in ("claude:9", "claude:0", "claude:05", "claude:a", "claude:"):
        with pytest.raises(account_login.LoginError):
            account_login.resolve_target(bad)


def test_bindings_list_slot5_before_its_home_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(account_login, "CLAUDE_SLOT_ROOT", tmp_path)
    monkeypatch.setattr(account_login, "KEEPER_LOG", tmp_path / "none.log")
    row = next(b for b in account_login.bindings() if b["target"] == "claude:5")
    assert row["needs_login"] is True and row["bound"] is False
    assert row["actual_account"] is None
    assert not (tmp_path / "slot5").exists()


def test_login_start_creates_slot_home_with_mode_700(tmp_path, monkeypatch):
    monkeypatch.setattr(account_login, "CLAUDE_SLOT_ROOT", tmp_path)
    monkeypatch.setattr(account_login, "CLAUDE_BIN", "/nonexistent/claude-for-test")
    with pytest.raises(account_login.LoginError):
        asyncio.run(account_login.start("claude:5"))
    home = tmp_path / "slot5"
    assert home.is_dir() and (home.stat().st_mode & 0o777) == 0o700
    assert list(home.iterdir()) == []


def _sess(tmp_path, slot, email):
    home = tmp_path / f"slot{slot}"
    home.mkdir(parents=True)
    if email is not None:
        (home / ".claude.json").write_text('{"oauthAccount": {"emailAddress": "%s"}}' % email)
    return {"target": f"claude:{slot}", "plan": {"home": home}}


def test_host_identity_gate_for_slot5(tmp_path):
    assert account_login._claude_identity_ok(_sess(tmp_path / "a", "5", "OBYS.aads@gmail.com")) is True
    assert account_login._claude_identity_ok(_sess(tmp_path / "b", "5", "other@example.com")) is False
    assert account_login._claude_identity_ok(_sess(tmp_path / "c", "5", None)) is False
    assert account_login._claude_identity_ok(_sess(tmp_path / "d", "2", "anyone@example.com")) is True


def test_sync_runs_keeper_for_the_requested_slot(monkeypatch):
    calls = []

    class Proc:
        async def wait(self):
            return 0

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return Proc()

    monkeypatch.setattr(account_login.asyncio, "create_subprocess_exec", create)
    assert asyncio.run(account_login._sync_slot_token_to_db("5")) is True
    assert calls[0][1]["env"]["CLAUDE_TOKEN_KEEPER_SLOT"] == "5"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Conn:
    def __init__(self, record):
        self.record, self.executed, self.fetch_args = record, [], None

    def transaction(self):
        return _Tx()

    async def fetchrow(self, *args):
        self.fetch_args = args
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


def _wire(monkeypatch, conn, account):
    monkeypatch.setattr(llm_keys, "get_pool", lambda: _Pool(conn))
    monkeypatch.setattr(llm_keys, "decrypt_value", lambda _v: TOKEN)
    monkeypatch.setattr(llm_keys, "invalidate_key_cache", lambda *_: None)
    monkeypatch.setattr(llm_keys, "invalidate_registry_cache", lambda: None)

    async def relay_call(_method, _path):
        return {"bindings": [{"target": "claude:5", "bound": True, "needs_login": False,
                              "actual_account": account}]}

    monkeypatch.setattr(llm_keys, "_relay_call", relay_call)


def _row(expires_in=timedelta(hours=1)):
    return {"key_name": "ANTHROPIC_AUTH_TOKEN_5", "label": "obys.aads@gmail.com",
            "encrypted_value": "cipher", "oauth_expires_at": datetime.now(timezone.utc) + expires_in}


def _login(fingerprint=None):
    return {"target": "claude:5",
            "credential_fingerprint": fingerprint or hashlib.sha256(TOKEN.encode()).hexdigest()}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["identity", "fingerprint", "expiry", "missing_row"])
async def test_slot5_failed_proof_never_activates(monkeypatch, failure):
    conn = _Conn(None if failure == "missing_row" else _row(
        timedelta(minutes=2) if failure == "expiry" else timedelta(hours=1)))
    _wire(monkeypatch, conn, "wrong@example.com" if failure == "identity" else "obys.aads@gmail.com")
    ok = await llm_keys._reconcile_successful_account_login(
        "login-id", _login("0" * 64 if failure == "fingerprint" else None))
    assert ok is False
    assert conn.executed == []


@pytest.mark.asyncio
async def test_slot5_verified_proof_activates_only_slot5_row(monkeypatch):
    conn = _Conn(_row())
    _wire(monkeypatch, conn, "obys.aads@gmail.com")
    assert await llm_keys._reconcile_successful_account_login("login-id", _login()) is True
    assert conn.fetch_args[-1] == "ANTHROPIC_AUTH_TOKEN_5"
    assert "is_active = NOT manual_disabled" in conn.executed[0][0]
    assert conn.executed[0][1] == ("ANTHROPIC_AUTH_TOKEN_5",)


@pytest.mark.asyncio
async def test_slot4_identity_is_still_pinned_to_its_own_account(monkeypatch):
    conn = _Conn({**_row(), "key_name": "ANTHROPIC_AUTH_TOKEN_4"})
    _wire(monkeypatch, conn, "obys.aads@gmail.com")

    async def relay_call(_m, _p):
        return {"bindings": [{"target": "claude:4", "bound": True, "needs_login": False,
                              "actual_account": "obys.aads@gmail.com"}]}

    monkeypatch.setattr(llm_keys, "_relay_call", relay_call)
    assert await llm_keys._reconcile_successful_account_login(
        "login-id", {**_login(), "target": "claude:4"}) is False
    assert conn.executed == []


def test_hardcoded_slot_lists_match_the_registry():
    slots = " ".join(claude_slots.CLAUDE_SLOTS)
    keeper = (ROOT / "scripts" / "claude_token_keeper.sh").read_text()
    assert re.search(r'CLAUDE_SLOTS="\$\{CLAUDE_TOKEN_KEEPER_SLOTS:-([0-9 ]+)\}"', keeper).group(1) == slots
    assert "for slot in $CLAUDE_SLOTS; do" in keeper
    key_for_slot = keeper.split("key_for_slot() {", 1)[1].split("}", 1)[0]
    assert "2|3|4|5)" in key_for_slot
    assert f'SLOTS="${{CLAUDE_LEASE_SLOTS:-{slots}}}"' in (ROOT / "scripts" / "claude-lease-push.sh").read_text()
    assert f'Environment="CLAUDE_LEASE_SLOTS={slots}"' in (
        ROOT / "scripts" / "aads-claude-lease-push.service").read_text()
    runner = (ROOT / "scripts" / "pipeline-runner.sh").read_text()
    for slot, key in claude_slots.SLOT_KEY_NAMES.items():
        assert f"WHEN '{key}'" in runner and f"THEN '{slot}'" in runner, key


def test_keeper_creates_missing_row_only_for_explicit_registered_slot():
    keeper = (ROOT / "scripts" / "claude_token_keeper.sh").read_text()
    assert 'os.getenv("AADS_KEEPER_CREATE_ROW") == "1"' in keeper
    assert '[ -n "$LOGIN_SYNC" ] && [ "$REQUESTED_SLOT" = "$slot" ] && create_row=1' in keeper
    assert "FALSE, NOW()" in keeper  # 행은 비활성으로 만든다
    assert claude_slots.SLOT_REGISTER_LABELS == {"5": "obys.aads@gmail.com"}
    assert claude_slots.SLOT_EXPECTED_ACCOUNTS["5"] == "obys.aads@gmail.com"
    assert "5" not in LAST_RESORT_SLOTS
