"""AADS-OBYS-VAULT-SEPARATION-20260930: 오비서는 AADS vault 키를 쓰지 않는다."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.core import credential_vault
from app.core.obys_runtime import RuntimeSettings

ROOT = Path(__file__).resolve().parents[2]
AADS_KEY = Fernet.generate_key()
OBYS_KEY = Fernet.generate_key()


@pytest.fixture(autouse=True)
def isolated_vault(monkeypatch, tmp_path):
    """모듈 전역 키 상태를 테스트마다 되돌린다. 키 파일은 tmp 로 돌린다."""
    monkeypatch.setattr(credential_vault, "_VAULT_KEY", None)
    monkeypatch.setattr(credential_vault, "_STANDALONE_KEY_SOURCE", None)
    monkeypatch.setattr(credential_vault, "_VAULT_KEY_FILE", str(tmp_path / "vault.key"))
    monkeypatch.delenv("VAULT_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("OBYS_VAULT_KEY", raising=False)
    yield


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "finance"
    uploads = tmp_path / "ledgers"
    data.mkdir()
    uploads.mkdir()
    return {
        "OBYS_AUTH_DATABASE_URL": "postgresql://obys@127.0.0.1/obys_identity",
        "OBYS_DATABASE_URL": "postgresql://obys@127.0.0.1/obys",
        "ACCT_DATABASE_URL": "postgresql://reader@127.0.0.1/acct",
        "JWT_SECRET_KEY": "test-only-key-that-is-at-least-32-bytes",
        "YEOLJEONG_FINANCE_DATA_DIR": str(data),
        "OBYS_UPLOAD_ROOT": str(uploads),
    }


def _apply(settings: RuntimeSettings, monkeypatch) -> None:
    # apply() 는 os.environ 에 DB 별칭을 쓴다. 테스트 밖으로 새지 않게 먼저 등록한다.
    for name in ("DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL"):
        monkeypatch.setenv(name, "unused")
    settings.apply()


# ── (a) standalone: AADS 키만 있으면 vault 비활성 ─────────────────

def test_standalone_ignores_aads_key_and_disables_vault(env, monkeypatch, caplog, tmp_path):
    env["VAULT_ENCRYPTION_KEY"] = AADS_KEY.decode()
    monkeypatch.setenv("VAULT_ENCRYPTION_KEY", AADS_KEY.decode())
    with caplog.at_level(logging.INFO):
        settings = RuntimeSettings.from_env(env)
        _apply(settings, monkeypatch)

    assert settings.vault_key is None and not settings.vault_enabled
    assert credential_vault.is_standalone_vault()
    assert not credential_vault.vault_enabled()
    with pytest.raises(credential_vault.VaultUnavailableError):
        credential_vault.encrypt_value("x")
    with pytest.raises(credential_vault.VaultUnavailableError):
        credential_vault.decrypt_value(Fernet(AADS_KEY).encrypt(b"x").decode())
    # 키 파일 자동 생성 경로도 타지 않는다.
    assert not (tmp_path / "vault.key").exists()
    assert AADS_KEY.decode() not in caplog.text


def test_standalone_malformed_key_disables_vault_without_blocking_startup(env, monkeypatch, caplog):
    env["OBYS_VAULT_KEY"] = "not-a-fernet-key-SECRETVALUE"
    with caplog.at_level(logging.WARNING):
        settings = RuntimeSettings.from_env(env)
    assert settings.vault_key is None
    assert "SECRETVALUE" not in caplog.text
    assert "OBYS_VAULT_KEY" in caplog.text


def test_standalone_key_is_not_in_repr_or_logs(env, monkeypatch, caplog):
    env["OBYS_VAULT_KEY"] = OBYS_KEY.decode()
    with caplog.at_level(logging.DEBUG):
        settings = RuntimeSettings.from_env(env)
        _apply(settings, monkeypatch)
    assert settings.vault_enabled
    assert OBYS_KEY.decode() not in repr(settings)
    assert OBYS_KEY.decode() not in caplog.text


@pytest.mark.asyncio
async def test_standalone_get_credential_refuses_when_disabled(monkeypatch):
    credential_vault.configure_vault_key(None)
    pool = MagicMock()
    monkeypatch.setattr(credential_vault, "get_pool", lambda: pool)
    with pytest.raises(credential_vault.VaultUnavailableError):
        await credential_vault.get_credential(str(uuid4()), tenant_id=str(uuid4()))
    pool.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_standalone_get_credential_skips_agent_vault_fallback(monkeypatch):
    credential_vault.configure_vault_key(OBYS_KEY)
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr(credential_vault, "get_pool", lambda: pool)
    assert await credential_vault.get_credential(str(uuid4()), tenant_id=str(uuid4())) is None
    assert pool.fetchrow.await_count == 1  # agent_vault_credentials 는 오비서 DB 에 없다


# ── (b) 두 키가 다르면 오비서는 AADS 암호문을 못 푼다 ────────────────

def test_obys_cannot_decrypt_aads_ciphertext(env, monkeypatch):
    aads_ciphertext = Fernet(AADS_KEY).encrypt(b"aads-only-secret").decode()
    env["OBYS_VAULT_KEY"] = OBYS_KEY.decode()
    monkeypatch.setenv("VAULT_ENCRYPTION_KEY", AADS_KEY.decode())
    _apply(RuntimeSettings.from_env(env), monkeypatch)

    with pytest.raises(InvalidToken):
        credential_vault.decrypt_value(aads_ciphertext)
    own = credential_vault.encrypt_value("obys-secret")
    assert credential_vault.decrypt_value(own) == "obys-secret"
    with pytest.raises(InvalidToken):
        Fernet(AADS_KEY).decrypt(own.encode())


# ── (c) AADS 모드 기존 동작 불변 ─────────────────────────────────

def test_aads_mode_reads_vault_encryption_key(monkeypatch):
    monkeypatch.setenv("VAULT_ENCRYPTION_KEY", AADS_KEY.decode())
    monkeypatch.setenv("OBYS_VAULT_KEY", OBYS_KEY.decode())  # AADS 는 이 값을 보지 않는다

    assert not credential_vault.is_standalone_vault()
    assert credential_vault.vault_enabled()
    token = credential_vault.encrypt_value("aads-secret")
    assert Fernet(AADS_KEY).decrypt(token.encode()) == b"aads-secret"
    assert credential_vault.decrypt_value(Fernet(AADS_KEY).encrypt(b"old").decode()) == "old"
    credential_vault.require_vault_enabled()  # 예외 없음


def test_aads_mode_key_file_fallback_is_unchanged(monkeypatch, tmp_path):
    key_file = tmp_path / "vault.key"
    key_file.write_text(AADS_KEY.decode())
    assert credential_vault.decrypt_value(Fernet(AADS_KEY).encrypt(b"f").decode()) == "f"


def test_aads_mode_auto_generates_key_file_when_missing(tmp_path):
    token = credential_vault.encrypt_value("g")
    assert (tmp_path / "vault.key").is_file()
    assert credential_vault.decrypt_value(token) == "g"


@pytest.mark.asyncio
async def test_aads_mode_get_credential_still_falls_back_to_agent_vault(monkeypatch):
    monkeypatch.setenv("VAULT_ENCRYPTION_KEY", AADS_KEY.decode())
    f = Fernet(AADS_KEY)
    cred_id, tenant_id = uuid4(), uuid4()
    agent_row = {
        "id": cred_id, "tenant_id": tenant_id, "label": "L", "origin": "https://x",
        "work_key": "svc", "username_enc": f.encrypt(b"u").decode(),
        "password_enc": f.encrypt(b"p").decode(), "metadata": {},
        "created_at": None, "updated_at": None, "last_used_at": None,
    }
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=[None, agent_row])
    monkeypatch.setattr(credential_vault, "get_pool", lambda: pool)

    item = await credential_vault.get_credential(str(cred_id), tenant_id=str(tenant_id))
    assert item["vault_source"] == "agent_vault"
    assert (item["username"], item["password"]) == ("u", "p")


def test_obys_runtime_never_reads_aads_key_name():
    source = (ROOT / "app/core/obys_runtime.py").read_text()
    code = [ln for ln in source.splitlines() if not ln.lstrip().startswith(("#", '"""'))]
    assert not any('"VAULT_ENCRYPTION_KEY"' in ln for ln in code)


# ── 마이그레이션 SQL / 이관 스크립트 ────────────────────────────

def test_migration_sql_is_idempotent_tenant_scoped_and_held_from_auto_apply():
    sql = (ROOT / "migrations/20260930_obys_e2e_credentials_jinah.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS public.e2e_credentials" in sql
    assert "tenant_id       UUID NOT NULL" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_cred_tenant_service_project_label" in sql
    assert "chat_sessions" in sql  # AADS DB 오적용 차단
    baseline = (ROOT / "scripts/migrations_auto_apply_baseline.txt").read_text().splitlines()
    assert "migrations/20260930_obys_e2e_credentials_jinah.sql" in baseline


def _load_migrate():
    spec = importlib.util.spec_from_file_location("obys_vault_migrate", ROOT / "scripts/obys_vault_migrate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(src: Fernet, **kw):
    row = {
        "id": uuid4(), "tenant_id": uuid4(), "service": "obys", "project": None,
        "label": "관리자", "login_url": "https://fb.newtalk.kr/login",
        "username_enc": src.encrypt(b"ceo@example.com").decode(),
        "password_enc": src.encrypt(b"PLAINTEXT-PW").decode(),
        "extra_fields": '{"otp": "%s", "source": "env"}' % src.encrypt(b"OTP-SEED").decode(),
        "login_steps": "[]", "is_active": True,
    }
    row.update(kw)
    return row


def test_migrate_selects_only_obys_domain_rows():
    m = _load_migrate()
    assert m.is_obys_row({"login_url": "https://fb.newtalk.kr/login"})
    assert m.is_obys_row({"login_url": "fb.newtalk.kr/x"})
    assert m.is_obys_row({"project": "food", "login_url": "https://other"})
    assert m.is_obys_row({"project": "ACCT"})
    assert not m.is_obys_row({"project": "AADS", "login_url": "https://aads.newtalk.kr"})
    assert not m.is_obys_row({"login_url": "https://evil.example/?next=fb.newtalk.kr"})
    assert not m.is_obys_row({"login_url": "https://fb.newtalk.kr.evil.example"})


def test_migrate_reencrypts_without_leaking_plaintext():
    m = _load_migrate()
    src, dst = Fernet(AADS_KEY), Fernet(OBYS_KEY)
    tenant = uuid4()
    rows = [
        _row(src, tenant_id=tenant),
        _row(src, tenant_id=tenant, project="AADS", login_url="https://aads.newtalk.kr"),
        _row(src, tenant_id=tenant, label="broken", password_enc="garbage"),
        _row(src, tenant_id=uuid4(), label="other-tenant"),
    ]
    planned = m.plan_rows(rows, src, dst, {str(tenant)}, set(), {})
    assert [e["action"] for e in planned] == ["insert", "skip", "skip"]
    assert planned[1]["reason"] == "source_decrypt_failed:password_enc"
    assert planned[2]["reason"] == "tenant_missing_in_target"

    moved = planned[0]["row"]
    assert dst.decrypt(moved["password_enc"].encode()) == b"PLAINTEXT-PW"
    assert dst.decrypt(moved["extra_fields"]["otp"].encode()) == b"OTP-SEED"
    assert moved["extra_fields"]["source"] == "env"
    with pytest.raises(InvalidToken):
        src.decrypt(moved["password_enc"].encode())

    printed = repr([m.public_view(e) for e in planned])
    for secret in ("PLAINTEXT-PW", "ceo@example.com", "OTP-SEED", moved["password_enc"]):
        assert secret not in printed


def test_migrate_plan_is_idempotent_by_id_and_guards_natural_key():
    m = _load_migrate()
    src, dst = Fernet(AADS_KEY), Fernet(OBYS_KEY)
    row = _row(src)
    tenants = {str(row["tenant_id"])}
    again = m.plan_rows([row], src, dst, tenants, {str(row["id"])}, {m.natural_key(row): str(row["id"])})
    assert again[0]["action"] == "update"
    clash = m.plan_rows([row], src, dst, tenants, set(), {m.natural_key(row): str(uuid4())})
    assert clash[0]["reason"] == "natural_key_taken_by_other_id"


def test_migrate_refuses_same_key_and_defaults_to_dry_run(tmp_path, capsys):
    m = _load_migrate()
    with pytest.raises(m.ConfigError, match="must differ"):
        m.load_keys({"VAULT_ENCRYPTION_KEY": AADS_KEY.decode(), "OBYS_VAULT_KEY": AADS_KEY.decode()}, "")
    with pytest.raises(m.ConfigError, match="OBYS_VAULT_KEY"):
        m.load_keys({"VAULT_ENCRYPTION_KEY": AADS_KEY.decode()}, "")
    key_file = tmp_path / "k"
    key_file.write_text(AADS_KEY.decode())
    src, dst = m.load_keys({"OBYS_VAULT_KEY": OBYS_KEY.decode()}, str(key_file))
    assert src.decrypt(Fernet(AADS_KEY).encrypt(b"z")) == b"z"
    assert m.main(["--source-dsn", ""]) == 2
    assert "postgresql" not in capsys.readouterr().err
