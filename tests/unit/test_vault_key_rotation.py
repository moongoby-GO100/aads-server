"""AADS-VAULT-KEY-ROTATION-20260930: VAULT_ENCRYPTION_KEY 무중단 교체(MultiFernet) + 재암호화."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core import credential_vault

ROOT = Path(__file__).resolve().parents[2]
NEW_KEY = Fernet.generate_key()
OLD_KEY = Fernet.generate_key()
OLDER_KEY = Fernet.generate_key()
OBYS_KEY = Fernet.generate_key()


@pytest.fixture(autouse=True)
def isolated_vault(monkeypatch, tmp_path):
    monkeypatch.setattr(credential_vault, "_VAULT_KEY", None)
    monkeypatch.setattr(credential_vault, "_VAULT_PREVIOUS_KEYS", ())
    monkeypatch.setattr(credential_vault, "_STANDALONE_KEY_SOURCE", None)
    monkeypatch.setattr(credential_vault, "_VAULT_KEY_FILE", str(tmp_path / "vault.key"))
    for name in ("VAULT_ENCRYPTION_KEY", "VAULT_ENCRYPTION_KEY_PREVIOUS", "OBYS_VAULT_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield


def _rotating_env(monkeypatch, previous: str) -> None:
    monkeypatch.setenv("VAULT_ENCRYPTION_KEY", NEW_KEY.decode())
    monkeypatch.setenv("VAULT_ENCRYPTION_KEY_PREVIOUS", previous)


# ── (a) 이전키 암호문도 읽힌다 ─────────────────────────────

def test_previous_key_ciphertext_decrypts_via_multifernet(monkeypatch):
    _rotating_env(monkeypatch, f"{OLD_KEY.decode()}, {OLDER_KEY.decode()}")
    assert isinstance(credential_vault._get_fernet(), MultiFernet)
    assert credential_vault.decrypt_value(Fernet(OLD_KEY).encrypt(b"old-secret").decode()) == "old-secret"
    assert credential_vault.decrypt_value(Fernet(OLDER_KEY).encrypt(b"older").decode()) == "older"


# ── (b) 새 암호화는 주키로만 ────────────────────────────────

def test_new_ciphertext_uses_primary_key_only(monkeypatch):
    _rotating_env(monkeypatch, OLD_KEY.decode())
    token = credential_vault.encrypt_value("fresh").encode()
    assert Fernet(NEW_KEY).decrypt(token) == b"fresh"
    with pytest.raises(InvalidToken):
        Fernet(OLD_KEY).decrypt(token)


# ── (c) 이전키 미설정 시 기존 단일 Fernet 동작 불변 ──────────────

def test_without_previous_key_behaviour_is_single_fernet(monkeypatch):
    monkeypatch.setenv("VAULT_ENCRYPTION_KEY", NEW_KEY.decode())
    fernet = credential_vault._get_fernet()
    assert type(fernet) is Fernet
    assert credential_vault._VAULT_PREVIOUS_KEYS == ()
    token = credential_vault.encrypt_value("same")
    assert Fernet(NEW_KEY).decrypt(token.encode()) == b"same"
    assert credential_vault.decrypt_value(Fernet(NEW_KEY).encrypt(b"x").decode()) == "x"
    with pytest.raises(InvalidToken):
        credential_vault.decrypt_value(Fernet(OLD_KEY).encrypt(b"x").decode())


def test_without_previous_key_file_fallback_unchanged(monkeypatch, tmp_path):
    key_file = tmp_path / "vault.key"
    key_file.write_text(OLD_KEY.decode())
    fernet = credential_vault._get_fernet()
    assert type(fernet) is Fernet
    assert credential_vault._VAULT_KEY == OLD_KEY


def test_empty_previous_env_is_single_fernet(monkeypatch):
    _rotating_env(monkeypatch, " , ")
    assert type(credential_vault._get_fernet()) is Fernet


# ── (d) standalone 은 MultiFernet 을 쓰지 않는다 — 회귀 방지 핵심 ──────

def test_standalone_ignores_previous_keys_and_uses_obys_key_only(monkeypatch):
    # AADS 쪽 키/이전키가 env 에 있어도 standalone 은 OBYS_VAULT_KEY 단일 키만 쓴다.
    _rotating_env(monkeypatch, OLD_KEY.decode())
    credential_vault._get_fernet()  # AADS 모드로 한 번 로드된 뒤 전환돼도 새지 않아야 한다.
    credential_vault.configure_vault_key(OBYS_KEY)
    assert credential_vault.is_standalone_vault()
    assert credential_vault._VAULT_PREVIOUS_KEYS == ()
    fernet = credential_vault._get_fernet()
    assert type(fernet) is Fernet
    token = credential_vault.encrypt_value("obys").encode()
    assert Fernet(OBYS_KEY).decrypt(token) == b"obys"
    for foreign in (OLD_KEY, NEW_KEY):
        with pytest.raises(InvalidToken):
            credential_vault.decrypt_value(Fernet(foreign).encrypt(b"x").decode())


def test_standalone_without_key_stays_disabled_with_previous_env(monkeypatch):
    _rotating_env(monkeypatch, OLD_KEY.decode())
    credential_vault.configure_vault_key(None)
    with pytest.raises(credential_vault.VaultUnavailableError):
        credential_vault.decrypt_value(Fernet(OLD_KEY).encrypt(b"x").decode())


# ── (e) 잘못된 이전키가 섞여도 기동된다 ──────────────────────────

def test_malformed_previous_key_is_skipped_without_leaking(monkeypatch, caplog):
    bad = "not-a-fernet-key-SECRETVALUE"
    _rotating_env(monkeypatch, f"{bad},{OLD_KEY.decode()},{NEW_KEY.decode()}")
    with caplog.at_level(logging.INFO):
        fernet = credential_vault._get_fernet()
    assert isinstance(fernet, MultiFernet)
    assert credential_vault._VAULT_PREVIOUS_KEYS == (OLD_KEY,)  # 주키 중복도 제외
    assert credential_vault.decrypt_value(Fernet(OLD_KEY).encrypt(b"ok").decode()) == "ok"
    assert credential_vault.decrypt_value(credential_vault.encrypt_value("new")) == "new"
    assert "vault_previous_key_invalid index=0" in caplog.text
    for secret in (bad, OLD_KEY.decode(), NEW_KEY.decode()):
        assert secret not in caplog.text


def test_only_malformed_previous_keys_fall_back_to_single_fernet(monkeypatch):
    _rotating_env(monkeypatch, "garbage")
    assert type(credential_vault._get_fernet()) is Fernet


# ── (f) 재암호화 스크립트 ─────────────────────────────────

def _load_script():
    path = ROOT / "scripts" / "vault_rotate_reencrypt.py"
    spec = importlib.util.spec_from_file_location("vault_rotate_reencrypt", path)
    module = importlib.util.module_from_spec(spec)
    # dataclass 가 모듈을 sys.modules 에서 찾는다.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _keys(m):
    return m.load_keys({
        "VAULT_ENCRYPTION_KEY": NEW_KEY.decode(),
        "VAULT_ENCRYPTION_KEY_PREVIOUS": OLD_KEY.decode(),
    })


class FakeConn:
    """rows 를 메모리에 두고 transaction 예외 시 스냅샷으로 되돌린다."""

    def __init__(self, table: str, rows: list[dict]):
        self.table = table
        self.rows = rows
        self.executed: list[str] = []
        self.readonly_flags: list[bool] = []

    @asynccontextmanager
    async def transaction(self, readonly: bool = False):
        self.readonly_flags.append(readonly)
        snapshot = copy.deepcopy(self.rows)
        try:
            yield
        except BaseException:
            self.rows[:] = snapshot
            raise

    async def fetch(self, sql: str):
        assert f"FROM {self.table}" in sql
        return [dict(r) for r in self.rows]

    async def execute(self, sql: str, row_id, *values):
        self.executed.append(sql)
        columns = [part.split(" = ")[0].strip() for part in sql.split(" SET ")[1].split(" WHERE ")[0].split(",")]
        for row in self.rows:
            if row["id"] == row_id:
                for column, value in zip(columns, values):
                    row[column] = value


def _e2e_rows():
    old, new = Fernet(OLD_KEY), Fernet(NEW_KEY)
    return [
        {
            "id": "r1",
            "username_enc": old.encrypt(b"user1").decode(),
            "password_enc": old.encrypt(b"pw1").decode(),
            "extra_fields": json.dumps({"otp": old.encrypt(b"otp1").decode(), "source": "manual"}),
        },
        {  # 이미 새 키 — 건너뛴다
            "id": "r2",
            "username_enc": new.encrypt(b"user2").decode(),
            "password_enc": new.encrypt(b"pw2").decode(),
            "extra_fields": json.dumps({}),
        },
        {  # 어떤 키로도 안 풀린다 — 손대지 않고 id 만
            "id": "r3",
            "username_enc": Fernet(OLDER_KEY).encrypt(b"x").decode(),
            "password_enc": Fernet(OLDER_KEY).encrypt(b"y").decode(),
            "extra_fields": None,
        },
    ]


def test_script_dry_run_does_not_write(capsys):
    m = _load_script()
    rows = _e2e_rows()
    before = copy.deepcopy(rows)
    conn = FakeConn("e2e_credentials", rows)
    summary = asyncio.run(m.rotate_table(conn, m.TABLES["e2e_credentials"], _keys(m), apply=False))
    assert summary["status"] == "ok" and summary["mode"] == "dry-run"
    assert (summary["rotate"], summary["current"], summary["undecryptable"]) == (1, 1, 1)
    assert summary["undecryptable_ids"] == ["r3"]
    assert conn.rows == before and conn.executed == [] and conn.readonly_flags == [True]


def test_script_apply_reencrypts_per_value_and_is_idempotent():
    m = _load_script()
    conn = FakeConn("e2e_credentials", _e2e_rows())
    spec, keys = m.TABLES["e2e_credentials"], _keys(m)
    summary = asyncio.run(m.rotate_table(conn, spec, keys, apply=True))
    assert summary["status"] == "ok" and summary["updated"] == 1
    r1 = conn.rows[0]
    new = Fernet(NEW_KEY)
    assert new.decrypt(r1["username_enc"].encode()) == b"user1"
    assert new.decrypt(r1["password_enc"].encode()) == b"pw1"
    extra = json.loads(r1["extra_fields"])
    assert new.decrypt(extra["otp"].encode()) == b"otp1"
    assert extra["source"] == "manual"
    with pytest.raises(InvalidToken):
        Fernet(OLD_KEY).decrypt(r1["password_enc"].encode())

    again = asyncio.run(m.rotate_table(conn, spec, keys, apply=True))
    assert again["status"] == "ok" and again["updated"] == 0 and again["rotate"] == 0


def test_script_verify_failure_rolls_back_whole_table_and_reports_ids_only(monkeypatch, capsys):
    m = _load_script()
    old = Fernet(OLD_KEY)
    rows = [
        {"id": 1, "encrypted_value": old.encrypt(b"sk-good-SECRET").decode()},
        {"id": 2, "encrypted_value": old.encrypt(b"sk-bad-SECRET").decode()},
    ]
    before = copy.deepcopy(rows)
    real_encrypt = m._encrypt

    def tampering_encrypt(primary, plain):
        return real_encrypt(primary, b"tampered" if b"bad" in plain else plain)

    monkeypatch.setattr(m, "_encrypt", tampering_encrypt)
    conn = FakeConn("llm_api_keys", rows)
    summary = asyncio.run(m.rotate_table(conn, m.TABLES["llm_api_keys"], _keys(m), apply=True))
    assert summary["status"] == "rolled_back" and summary["updated"] == 0
    assert summary["failed_ids"] == ["2"]
    assert conn.rows == before and conn.executed == []
    printed = json.dumps(summary)
    assert "SECRET" not in printed and before[1]["encrypted_value"] not in printed


def test_script_post_update_verify_failure_rolls_back(monkeypatch):
    m = _load_script()
    old = Fernet(OLD_KEY)
    rows = [{"id": "u1", "encrypted_key": old.encrypt(b"k1").decode()},
            {"id": "u2", "encrypted_key": old.encrypt(b"k2").decode()}]
    before = copy.deepcopy(rows)
    conn = FakeConn("user_api_keys", rows)
    real_execute = conn.execute

    async def corrupting_execute(sql, row_id, *values):
        await real_execute(sql, row_id, *values)
        if row_id == "u2":  # DB 에 저장된 값이 달라진 상황
            conn.rows[1]["encrypted_key"] = Fernet(NEW_KEY).encrypt(b"other").decode()

    monkeypatch.setattr(conn, "execute", corrupting_execute)
    summary = asyncio.run(m.rotate_table(conn, m.TABLES["user_api_keys"], _keys(m), apply=True))
    assert summary["status"] == "rolled_back" and summary["failed_ids"] == ["u2"]
    assert conn.rows == before


def test_script_config_errors_do_not_leak(capsys, monkeypatch):
    m = _load_script()
    with pytest.raises(m.ConfigError, match="VAULT_ENCRYPTION_KEY_PREVIOUS"):
        m.load_keys({"VAULT_ENCRYPTION_KEY": NEW_KEY.decode()})
    with pytest.raises(m.ConfigError, match="VAULT_ENCRYPTION_KEY_PREVIOUS"):
        m.load_keys({"VAULT_ENCRYPTION_KEY": NEW_KEY.decode(), "VAULT_ENCRYPTION_KEY_PREVIOUS": NEW_KEY.decode()})
    with pytest.raises(m.ConfigError, match="not a Fernet key"):
        m.load_keys({"VAULT_ENCRYPTION_KEY": "SECRETVALUE"})
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert m.main(["--dsn", ""]) == 2
    assert m.main(["--dsn", "postgresql://u:pw@h/db"]) == 2  # 키 없음 → 접속 전에 종료
    err = capsys.readouterr().err
    assert "postgresql" not in err and "SECRETVALUE" not in err
    assert set(m.TABLES) == {"agent_vault_credentials", "e2e_credentials", "llm_api_keys", "user_api_keys"}
