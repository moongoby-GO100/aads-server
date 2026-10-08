"""카페24 오비서의 클로브 저장소: 저장소 선택 분기, 콜백 라우트 권한, 마이그레이션 SQL 안전성."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import credential_vault
from app.services import clobe_mcp_client as clobe

ROOT = Path(__file__).parents[2]
MIGRATION = ROOT / "migrations/20261008_obys_clobe_mcp_store.sql"
CALLBACK = "/api/v1/integrations/clobe/oauth/callback"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("CLOBE_STORE_DATABASE_URL", "OBYS_DATABASE_URL", "OBYS_PUBLIC_BASE_URL",
                 "CLOBE_OAUTH_REDIRECT_URI", "AADS_PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(credential_vault, "_STANDALONE_KEY_SOURCE", None)
    monkeypatch.setattr(credential_vault, "_VAULT_KEY", None)
    monkeypatch.setattr(clobe, "_store_pool", None)
    monkeypatch.setattr(clobe, "_schema_ready", False)


def _standalone(monkeypatch, key: bytes | None = b"x"):
    monkeypatch.setattr(credential_vault, "_STANDALONE_KEY_SOURCE", "OBYS_VAULT_KEY")
    monkeypatch.setattr(credential_vault, "_VAULT_KEY", key)


class _FakePool:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.queried = []

    async def fetchval(self, sql, *args):
        self.queried.append(args[0])
        return args[0].removeprefix("public.") in self.existing


# ── 저장소 선택 ──────────────────────────────────────────

def test_default_mode_is_aads_and_uses_the_shared_pool(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(clobe, "get_pool", lambda: sentinel)
    assert clobe.store_mode() == clobe.STORE_AADS

    import asyncio

    assert asyncio.run(clobe._pool()) is sentinel


def test_standalone_runtime_selects_obys_store_and_never_the_shared_pool(monkeypatch):
    _standalone(monkeypatch)
    monkeypatch.setattr(clobe, "get_pool", lambda: pytest.fail("standalone must not touch the shared (auth DB) pool"))
    created = {}

    class _Asyncpg:
        @staticmethod
        async def create_pool(dsn, **kw):
            created["dsn"], created["kw"] = dsn, kw
            return "obys-pool"

    monkeypatch.setitem(__import__("sys").modules, "asyncpg", _Asyncpg)
    monkeypatch.setenv("OBYS_DATABASE_URL", "postgresql://runtime@127.0.0.1:5432/obys")
    monkeypatch.setenv("DATABASE_URL", "postgresql://auth@127.0.0.1:5432/auth")

    import asyncio

    async def twice():
        return await clobe._pool(), await clobe._pool()

    first, second = asyncio.run(twice())
    assert first == second == "obys-pool"
    assert created["dsn"] == "postgresql://runtime@127.0.0.1:5432/obys"
    assert created["kw"]["max_size"] >= 2  # refresh 락 커넥션 + 조회용


def test_explicit_store_url_wins_and_forces_obys_mode(monkeypatch):
    monkeypatch.setenv("CLOBE_STORE_DATABASE_URL", "postgresql://store@127.0.0.1:5432/other")
    monkeypatch.setenv("OBYS_DATABASE_URL", "postgresql://runtime@127.0.0.1:5432/obys")
    assert clobe.store_mode() == clobe.STORE_OBYS
    assert clobe.store_database_url().endswith("/other")


def test_obys_mode_without_a_database_url_fails_closed_instead_of_falling_back(monkeypatch):
    _standalone(monkeypatch)
    monkeypatch.setattr(clobe, "get_pool", lambda: pytest.fail("no fallback to the AADS pool"))
    with pytest.raises(clobe.ClobeError, match="store_database_url_missing"):
        clobe.store_database_url()

    import asyncio

    with pytest.raises(clobe.ClobeError):
        asyncio.run(clobe._pool())


@pytest.mark.asyncio
async def test_obys_mode_ensure_schema_checks_tables_and_never_runs_ddl(monkeypatch):
    _standalone(monkeypatch)
    pool = _FakePool(existing=clobe.STORE_TABLES)

    async def fake_pool():
        return pool

    monkeypatch.setattr(clobe, "_pool", fake_pool)
    await clobe.ensure_schema()
    assert clobe._schema_ready is True
    assert not hasattr(pool, "acquire")  # DDL 경로(acquire + SCHEMA_DDL)를 타지 않았다


@pytest.mark.asyncio
async def test_obys_mode_missing_tables_is_a_classified_error(monkeypatch):
    _standalone(monkeypatch)
    pool = _FakePool(existing=["clobe_mcp_connection"])

    async def fake_pool():
        return pool

    monkeypatch.setattr(clobe, "_pool", fake_pool)
    with pytest.raises(clobe.ClobeError, match="store_schema_missing"):
        await clobe.ensure_schema()
    assert clobe._schema_ready is False


# ── 리디렉트 URI ─────────────────────────────────────────

def test_aads_redirect_uri_is_unchanged(monkeypatch):
    assert clobe.redirect_uri() == "https://aads.newtalk.kr/api/v1/integrations/clobe/oauth/callback"
    monkeypatch.setenv("AADS_PUBLIC_BASE_URL", "https://x.example/")
    assert clobe.redirect_uri() == "https://x.example/api/v1/integrations/clobe/oauth/callback"


def test_obys_redirect_uri_defaults_to_fb_and_ignores_the_aads_base(monkeypatch):
    _standalone(monkeypatch)
    monkeypatch.setenv("AADS_PUBLIC_BASE_URL", "https://aads.newtalk.kr")
    assert clobe.redirect_uri() == "https://fb.newtalk.kr" + CALLBACK
    monkeypatch.setenv("OBYS_PUBLIC_BASE_URL", "https://fb2.example/")
    assert clobe.redirect_uri() == "https://fb2.example" + CALLBACK
    monkeypatch.setenv("CLOBE_OAUTH_REDIRECT_URI", "https://explicit.example/cb")
    assert clobe.redirect_uri() == "https://explicit.example/cb"


# ── 금고 키 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_standalone_without_vault_key_is_a_classified_error_before_any_db_access(monkeypatch):
    _standalone(monkeypatch, key=None)

    async def boom():
        pytest.fail("must not reach the store without a vault key")

    monkeypatch.setattr(clobe, "_pool", boom)
    with pytest.raises(clobe.ClobeError, match="vault_disabled"):
        await clobe.start_authorization("admin")
    with pytest.raises(clobe.ClobeError, match="vault_disabled"):
        await clobe.handle_callback(code="c", state="s")
    with pytest.raises(clobe.ClobeError, match="vault_disabled"):
        await clobe._valid_access_token()


@pytest.mark.asyncio
async def test_aads_mode_does_not_require_the_standalone_vault_key():
    clobe._require_vault()  # 예외 없음


# ── 오비서 앱의 콜백 라우트 ───────────────────────────────

@pytest.fixture
def obys_client(monkeypatch):
    from app import yeoljeong_main

    async def fake_callback(*, code, state, error=None):
        if state != "good":
            raise clobe.ClobeError("state_invalid_or_expired")
        return {"status": "connected"}

    monkeypatch.setattr(clobe, "handle_callback", fake_callback)
    return TestClient(yeoljeong_main.app, raise_server_exceptions=False)


def test_obys_app_mounts_the_clobe_router_and_callback_needs_no_jwt(obys_client):
    ok = obys_client.get(CALLBACK, params={"code": "c", "state": "good"})
    assert ok.status_code == 200
    bad = obys_client.get(CALLBACK, params={"code": "c", "state": "forged"})
    assert bad.status_code == 400
    assert "forged" not in bad.text


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/v1/integrations/clobe/oauth/start"),
        ("post", "/api/v1/integrations/clobe/reauth"),
        ("get", "/api/v1/integrations/clobe/status"),
        ("post", "/api/v1/integrations/clobe/verify"),
        ("post", "/api/v1/integrations/clobe/tools/refresh"),
        ("post", "/api/v1/integrations/clobe/revoke"),
        ("get", CALLBACK + "/"),
    ],
)
def test_every_other_clobe_route_still_requires_authentication(obys_client, method, path):
    assert getattr(obys_client, method)(path).status_code == 401


def test_only_the_exact_callback_path_is_exempt():
    from app import yeoljeong_main

    assert yeoljeong_main._AUTH_EXEMPT_EXACT_PATHS == {CALLBACK}
    assert not any(p.startswith("/api/v1/integrations") for p in yeoljeong_main._AUTH_EXEMPT_PREFIXES)


def test_non_callback_clobe_routes_depend_on_the_internal_admin_guard():
    from app.api import clobe_integration
    from app.auth import require_internal_admin

    for route in clobe_integration.router.routes:
        if route.path.endswith("/oauth/callback"):
            continue
        deps = {d.call for d in route.dependant.dependencies}
        assert require_internal_admin in deps, route.path


# ── 마이그레이션 SQL ─────────────────────────────────────

def _sql_without_comments() -> str:
    text = re.sub(r"/\*.*?\*/", " ", MIGRATION.read_text(), flags=re.S)
    return re.sub(r"--[^\n]*", " ", text)


def test_migration_is_transactional_and_has_no_destructive_statements():
    sql = _sql_without_comments()
    for word in (r"\bDROP\b", r"\bTRUNCATE\b", r"\bDELETE\b", r"\bALTER\b"):
        assert not re.search(word, sql, re.I), word
    assert re.search(r"^\s*BEGIN\s*;", sql, re.I | re.M) and re.search(r"^\s*COMMIT\s*;", sql, re.I | re.M)


def test_migration_creates_exactly_the_four_client_tables_idempotently():
    sql = _sql_without_comments()
    created = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql)
    assert tuple(created) == clobe.STORE_TABLES
    assert not re.search(r"CREATE TABLE (?!IF NOT EXISTS)", sql)
    assert "CREATE TABLE IF NOT EXISTS" in clobe.SCHEMA_DDL
    for name in created:
        assert name in clobe.SCHEMA_DDL


def test_migration_grants_are_minimal_and_only_to_the_runtime_role():
    sql = _sql_without_comments()
    grants = re.findall(r"GRANT\s+(.+?)\s+ON\s+(.+?)\s+TO\s+(\w+)\s*;", sql, re.S | re.I)
    assert grants
    tables_granted = set()
    for privs, tables, role in grants:
        assert role == "acct_business_runtime_r5"
        assert {p.strip().upper() for p in privs.split(",")} <= {"SELECT", "INSERT", "UPDATE"}
        tables_granted |= {t.strip().removeprefix("public.") for t in tables.split(",")}
    assert tables_granted == {
        "obys_clobe_company_link", "obys_clobe_collection_lease", "obys_clobe_collection_run",
        "obys_clobe_collection_state", "obys_clobe_item", "obys_clobe_ledger_entry",
        *clobe.STORE_TABLES,
    }
    assert not re.search(r"TO\s+PUBLIC\b|WITH GRANT OPTION|ALL PRIVILEGES|\bALL\b", sql, re.I)


def test_migration_skips_grants_when_the_role_is_absent():
    sql = _sql_without_comments()
    assert "pg_roles WHERE rolname = 'acct_business_runtime_r5'" in sql
    assert "grants skipped" in sql


def test_migration_is_in_the_cafe24_allowlist_with_a_probe_and_a_rollback():
    script = (ROOT / "scripts/deploy_acct_app_cafe24.sh").read_text()
    assert re.search(r"^\s+20261008_obys_clobe_mcp_store\.sql$", script, re.M)
    assert re.search(r"^\s+20261008_obys_clobe_mcp_store\.sql\)", script, re.M)  # probe
    down = (ROOT / "migrations/rollback/20261008_obys_clobe_mcp_store.down.sql").read_text()
    assert "REVOKE" in down and "DROP" not in down.upper()


def test_no_secret_values_in_the_new_files():
    token_re = re.compile(r"sk-ant-|gAAAA[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{43}=")
    for path in (MIGRATION, ROOT / "migrations/rollback/20261008_obys_clobe_mcp_store.down.sql"):
        assert not token_re.search(path.read_text()), path
