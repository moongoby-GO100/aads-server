from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app.core.obys_runtime import RuntimeConfigurationError, RuntimeSettings


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


def test_explicit_local_configuration(env):
    settings = RuntimeSettings.from_env(env)
    assert settings.upload_dir == Path(env["OBYS_UPLOAD_ROOT"])
    assert "postgresql" not in repr(settings)


@pytest.mark.parametrize("name", ["OBYS_AUTH_DATABASE_URL", "OBYS_DATABASE_URL", "ACCT_DATABASE_URL"])
@pytest.mark.parametrize("dsn", [
    "", "postgresql://user:PRIVATE@aads-postgres/aads",
    "postgresql://user:PRIVATE@5.104.86.116/aads",
    "postgresql://user:PRIVATE@127.0.0.1/auth?host=remote",
    "postgresql://user:PRIVATE@127.0.0.1/auth?port=9999",
    "postgresql://user:PRIVATE@127.0.0.1:9999/auth",
    "postgresql://user:PRIVATE@127.0.0.1/auth?dbname=aads",
    "postgresql://[broken",
])
def test_rejects_missing_remote_or_override_dsn_without_leaking(env, name, dsn):
    env[name] = dsn
    with pytest.raises(RuntimeConfigurationError) as caught:
        RuntimeSettings.from_env(env)
    assert "PRIVATE" not in str(caught.value)


def test_socket_connection_supported(env):
    env["OBYS_AUTH_DATABASE_URL"] = "postgresql:///obys_identity?host=/var/run/postgresql"
    assert RuntimeSettings.from_env(env)


def test_same_database_even_via_different_loopback_alias_is_rejected(env):
    env["OBYS_AUTH_DATABASE_URL"] = "postgresql://other@localhost/obys"
    with pytest.raises(RuntimeConfigurationError, match="distinct"):
        RuntimeSettings.from_env(env)


@pytest.mark.parametrize("name", ["DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL"])
def test_inherited_aads_alias_is_rejected(env, name):
    env[name] = "postgresql://aads:SECRET@old/aads"
    with pytest.raises(RuntimeConfigurationError, match="conflicts") as caught:
        RuntimeSettings.from_env(env)
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("value", ["", "short"])
def test_key_required(env, value):
    env["JWT_SECRET_KEY"] = value
    with pytest.raises(RuntimeConfigurationError, match="JWT_SECRET_KEY"):
        RuntimeSettings.from_env(env)


@pytest.mark.parametrize("value", ["", "relative/path", "/", "/missing-obys-dir"])
def test_persistence_must_be_explicit_existing_and_absolute(env, value):
    env["OBYS_UPLOAD_ROOT"] = value
    with pytest.raises(RuntimeConfigurationError):
        RuntimeSettings.from_env(env)


def test_release_or_symlink_into_release_cannot_be_upload_storage(env, tmp_path):
    release = Path(__file__).resolve().parents[2]
    link = tmp_path / "link"
    link.symlink_to(release / "app")
    for path in (release, link):
        env["OBYS_UPLOAD_ROOT"] = str(path)
        with pytest.raises(RuntimeConfigurationError, match="release"):
            RuntimeSettings.from_env(env)


def test_upload_root_is_explicit_and_legacy_default_is_preserved(env, monkeypatch):
    import app.services.obys_upload_service as uploads

    original = uploads.UPLOAD_ROOT
    try:
        monkeypatch.setenv("OBYS_UPLOAD_ROOT", env["OBYS_UPLOAD_ROOT"])
        assert importlib.reload(uploads).UPLOAD_ROOT == Path(env["OBYS_UPLOAD_ROOT"])
        monkeypatch.delenv("OBYS_UPLOAD_ROOT")
        assert importlib.reload(uploads).UPLOAD_ROOT == Path("app/data/yeoljeong_finance/uploads/ledgers")
    finally:
        uploads.UPLOAD_ROOT = original
