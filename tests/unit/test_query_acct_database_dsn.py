"""query_acct_database 접속 경로 선택(DSN > DSN(구) > SSH 터널)과 실패 로그."""
import asyncio
import logging
import sys
import types

import pytest

from app.api import ceo_chat_tools_db as db

_ENV_NAMES = (
    "ACCT_APP_DATABASE_URL",
    "ACCT_DATABASE_URL",
    "ACCT_DB_APP_USER",
    "ACCT_DB_APP_PASSWORD",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_unconfigured_when_nothing_set():
    route = db._resolve_acct_connection()
    assert route["mode"] == "unconfigured"
    assert route["dsn"] is None


def test_app_dsn_beats_legacy_dsn_and_tunnel_creds(monkeypatch):
    monkeypatch.setenv("ACCT_APP_DATABASE_URL", "postgresql://acct_admin:pw@114.207.244.86:5433/obys")
    monkeypatch.setenv("ACCT_DATABASE_URL", "postgresql://old:pw@5.104.85.244:5432/acct")
    monkeypatch.setenv("ACCT_DB_APP_USER", "u")
    monkeypatch.setenv("ACCT_DB_APP_PASSWORD", "p")
    route = db._resolve_acct_connection()
    assert route["mode"] == "ACCT_APP_DATABASE_URL"
    assert route["dsn"].startswith("postgresql://acct_admin:")
    assert route["target"] == "114.207.244.86:5433"


def test_legacy_dsn_used_when_app_dsn_blank(monkeypatch):
    monkeypatch.setenv("ACCT_APP_DATABASE_URL", "   ")
    monkeypatch.setenv("ACCT_DATABASE_URL", "postgresql://u:pw@10.0.0.5/acct")
    route = db._resolve_acct_connection()
    assert route["mode"] == "ACCT_DATABASE_URL"
    assert route["target"] == "10.0.0.5:5432"


def test_tunnel_mode_requires_both_credentials(monkeypatch):
    monkeypatch.setenv("ACCT_DB_APP_USER", "u")
    assert db._resolve_acct_connection()["mode"] == "unconfigured"
    monkeypatch.setenv("ACCT_DB_APP_PASSWORD", "p")
    route = db._resolve_acct_connection()
    assert route["mode"] == "ssh_tunnel"
    assert route["dsn"] is None
    assert "->" in route["target"]


@pytest.mark.parametrize("dsn", ["postgresql://u:s3cret@h.example:6543/db", "postgres://u:s3cret@h.example:6543/db?x=1"])
def test_dsn_host_port_hides_credentials(dsn):
    out = db._dsn_host_port(dsn)
    assert out == "h.example:6543"
    assert "s3cret" not in out


def test_connect_failure_logs_target_without_secrets(monkeypatch, caplog):
    monkeypatch.setenv("ACCT_APP_DATABASE_URL", "postgresql://acct_admin:s3cret@114.207.244.86:5433/obys")

    async def _boom(*args, **kwargs):
        raise OSError("connect failed")

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=_boom))

    with caplog.at_level(logging.ERROR, logger=db.logger.name):
        result = asyncio.run(db.query_acct_database("SELECT 1"))

    assert "ACCT DB 연결 실패" in result["error"]
    assert "114.207.244.86:5433" in caplog.text
    assert "s3cret" not in caplog.text
    assert "s3cret" not in result["error"]


def test_unconfigured_error_mentions_new_variable():
    result = asyncio.run(db.query_acct_database("SELECT 1"))
    assert "ACCT_APP_DATABASE_URL" in result["error"]
