"""db_safe_write Vault 보호(P1-A)·단일 문장 선차단(P1-B)을 실제 PostgreSQL 로 검증한다.

격리된 일회용 DB 전용이다. 운영 DB 에 돌리지 않는다.
  AADS_VAULT_GUARD_TEST_DATABASE_URL=postgresql://u:p@127.0.0.1:5432/<이름>_test
호스트가 127.0.0.1/localhost 가 아니거나 DB 이름이 _test 로 끝나지 않으면 건너뛴다.
각 테스트는 public 스키마의 대상 테이블을 지우고 새로 만든다.

자격증명은 테스트마다 새로 만든 Fernet 키로 암호화한 가짜 값이다. 출력에는
계정 id 와 상태만 찍고 이메일·암호문은 찍지 않는다.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

asyncpg = pytest.importorskip("asyncpg")

URL = os.getenv("AADS_VAULT_GUARD_TEST_DATABASE_URL", "")
_parsed = urlparse(URL)
pytestmark = pytest.mark.skipif(
    not URL
    or _parsed.hostname not in ("127.0.0.1", "localhost")
    or not _parsed.path.lstrip("/").endswith("_test"),
    reason="격리 PostgreSQL(127.0.0.1, *_test) 미설정",
)

T_A = "00000000-0000-0000-0000-00000000000a"  # Vault 를 저장한 조직
T_B = "00000000-0000-0000-0000-00000000000b"  # 교차 조직 계정의 실제 소속
T_C = "00000000-0000-0000-0000-00000000000c"  # 보호 계정이 없는 조직

INTERNAL = "https://aads.newtalk.kr/login"
EXTERNAL = "https://shop.external.example/login"

SCHEMA = """
DROP TABLE IF EXISTS public.agent_vault_credentials, public.e2e_credentials,
    public.tenant_memberships, public.misc_notes CASCADE;
DROP TABLE IF EXISTS public.saas_users, public.tenants CASCADE;
CREATE TABLE public.tenants (
    id uuid PRIMARY KEY, slug text UNIQUE NOT NULL, name text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','archived')),
    deleted_at timestamptz);
CREATE TABLE public.saas_users (
    id text PRIMARY KEY, email text UNIQUE NOT NULL, password_hash text NOT NULL DEFAULT 'x',
    name text, is_active boolean DEFAULT true, default_tenant_id uuid,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','deleted')),
    deleted_at timestamptz);
CREATE TABLE public.tenant_memberships (
    id serial PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES public.saas_users(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','invited','suspended','removed')),
    deleted_at timestamptz, UNIQUE (tenant_id, user_id));
CREATE TABLE public.agent_vault_credentials (
    id serial PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    work_key text NOT NULL DEFAULT 'w', origin text NOT NULL, label text NOT NULL DEFAULT 'd',
    username_enc text NOT NULL, password_enc text NOT NULL,
    is_active boolean NOT NULL DEFAULT true, created_by text NOT NULL DEFAULT '');
CREATE TABLE public.e2e_credentials (
    id serial PRIMARY KEY, service varchar(100) NOT NULL, project varchar(20),
    label varchar(100) NOT NULL DEFAULT '기본', login_url text,
    username_enc text NOT NULL, password_enc text NOT NULL,
    is_active boolean DEFAULT true, tenant_id uuid REFERENCES public.tenants(id));
CREATE TABLE public.misc_notes (id int PRIMARY KEY, body text);
"""

# id, email(저장값), default_tenant, 멤버십 조직
USERS = [
    ("u_cross", "cross@b.example", T_B, T_B),      # T_A Vault 에 저장된 T_B 계정 (P1-A)
    ("u_e2e", "e2e@a.example", T_A, T_A),          # e2e_credentials 참조
    ("u_mixed", "mixed@b.example", T_B, T_B),      # Vault 에 " MIXED@B.Example " 로 저장
    ("u_inactive", "inactive@c.example", T_C, T_C),  # is_active=false 자격증명만 있음
    ("u_ext", "ext@c.example", T_C, T_C),          # 외부 origin 자격증명만 있음
    ("u_free", "free@c.example", T_C, T_C),
    ("u_free2", "free2@c.example", T_C, T_C),
    ("u_ctrl", "ctrl@c.example", T_C, T_C),
]
PROTECTED = ("u_cross", "u_e2e", "u_mixed")


@pytest.fixture
def vault_key(monkeypatch):
    from cryptography.fernet import Fernet

    from app.core import credential_vault

    monkeypatch.setattr(credential_vault, "_VAULT_KEY", Fernet.generate_key())
    monkeypatch.setattr(credential_vault, "_VAULT_PREVIOUS_KEYS", ())
    monkeypatch.setattr(credential_vault, "_STANDALONE_KEY_SOURCE", None)
    monkeypatch.delenv("AADS_VAULT_GUARD_INTERNAL_HOSTS", raising=False)
    monkeypatch.delenv("AADS_PUBLIC_BASE_URL", raising=False)
    return credential_vault.encrypt_value


@asynccontextmanager
async def environment(encrypt):
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=3, timeout=10)
    try:
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA)
            for tid, slug in ((T_A, "a"), (T_B, "b"), (T_C, "c")):
                await conn.execute(
                    "INSERT INTO public.tenants(id, slug, name) VALUES ($1::uuid, $2, $2)", tid, slug)
            for uid, email, tenant, member in USERS:
                await conn.execute(
                    "INSERT INTO public.saas_users(id, email, default_tenant_id) VALUES ($1,$2,$3::uuid)",
                    uid, email, tenant)
                await conn.execute(
                    "INSERT INTO public.tenant_memberships(tenant_id, user_id) VALUES ($1::uuid,$2)",
                    member, uid)
            pw = encrypt("not-a-real-password")
            # 교차 조직: 조직 A 의 Vault 에 조직 B 계정. created_by 는 다른 사람이다.
            await conn.execute(
                "INSERT INTO public.agent_vault_credentials(tenant_id, origin, username_enc, password_enc, created_by)"
                " VALUES ($1::uuid, $2, $3, $4, 'u_free')", T_A, INTERNAL, encrypt("cross@b.example"), pw)
            await conn.execute(
                "INSERT INTO public.agent_vault_credentials(tenant_id, origin, username_enc, password_enc)"
                " VALUES ($1::uuid, $2, $3, $4)", T_C, EXTERNAL, encrypt("ext@c.example"), pw)
            await conn.execute(
                "INSERT INTO public.e2e_credentials(service, login_url, username_enc, password_enc, tenant_id)"
                " VALUES ('aads-dashboard', $1, $2, $3, $4::uuid)", INTERNAL, encrypt("e2e@a.example"), pw, T_A)
            await conn.execute(
                "INSERT INTO public.e2e_credentials(service, label, login_url, username_enc, password_enc, tenant_id)"
                " VALUES ('aads-dashboard', 'mixed', NULL, $1, $2, $3::uuid)",
                encrypt("  MIXED@B.Example \n"), pw, T_C)
            await conn.execute(
                "INSERT INTO public.e2e_credentials(service, label, login_url, username_enc, password_enc,"
                " is_active, tenant_id) VALUES ('aads-dashboard', 'old', $1, $2, $3, false, $4::uuid)",
                INTERNAL, encrypt("inactive@c.example"), pw, T_C)
            await conn.execute("INSERT INTO public.misc_notes VALUES (1, 'n')")
        from app.services.tool_executor import ToolExecutor

        with patch("app.core.db_pool.get_pool", return_value=pool):
            yield pool, ToolExecutor()
    finally:
        await pool.close()


async def dump(pool) -> dict:
    """대상 테이블 전체 상태(부작용 비교용). 암호문 컬럼은 넣지 않는다."""
    async with pool.acquire() as conn:
        return {
            "users": [tuple(r) for r in await conn.fetch(
                "SELECT id, email, status, is_active, deleted_at, default_tenant_id"
                " FROM public.saas_users ORDER BY id")],
            "members": [tuple(r) for r in await conn.fetch(
                "SELECT user_id, tenant_id, status, deleted_at FROM public.tenant_memberships"
                " ORDER BY user_id, tenant_id")],
            "tenants": [tuple(r) for r in await conn.fetch(
                "SELECT id, status, deleted_at FROM public.tenants ORDER BY id")],
            "notes": [tuple(r) for r in await conn.fetch("SELECT * FROM public.misc_notes")],
        }


async def protected_rows(pool) -> list[tuple]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT u.id, u.status, u.is_active, u.deleted_at IS NULL AS alive,"
            " m.status AS membership, t.status AS tenant"
            " FROM public.saas_users u"
            " JOIN public.tenant_memberships m ON m.user_id = u.id"
            " JOIN public.tenants t ON t.id = u.default_tenant_id"
            " WHERE u.id = ANY($1::text[]) ORDER BY u.id", list(PROTECTED))
    return [tuple(r) for r in rows]


def show(label, rows):
    print(f"  [{label}]")
    for row in rows:
        print("    ", row)


async def both(executor, pool, sql, params=None):
    """dry_run → 실행 순서로 호출하고 dry_run 이 부작용 0 인지 확인한다."""
    before = await dump(pool)
    dry = await executor._db_safe_write({"sql": sql, "params": params or [], "dry_run": True})
    assert await dump(pool) == before, f"dry_run 부작용: {sql}"
    real = await executor._db_safe_write({"sql": sql, "params": params or []})
    # dry-run 동등성: 같은 보호 판정.
    assert bool(dry.get("blocked")) == bool(real.get("blocked")), (dry, real)
    assert dry.get("success") == real.get("success"), (dry, real)
    return dry, real, before


BLOCKED_CASES = [
    # P1-A 교차 조직, schema-qualified + alias, soft delete
    ("UPDATE public.saas_users AS u SET deleted_at = NOW() WHERE u.id = $1", ["u_cross"]),
    # alias 없이 비한정 이름, status 비활성 전이
    ("UPDATE saas_users SET status = 'deleted' WHERE id = 'u_cross'", None),
    ("UPDATE saas_users su SET status = 'suspended' WHERE su.id = $1", ["u_e2e"]),
    ("UPDATE public.saas_users SET is_active = false WHERE id = $1", ["u_e2e"]),
    # 이메일 정규화(저장값 대소문자·공백)
    ("UPDATE public.saas_users u SET deleted_at = NOW() WHERE u.id = 'u_mixed'", None),
    # 하드 삭제
    ('DELETE FROM "public"."saas_users" x WHERE x.id = $1', ["u_e2e"]),
    # 멤버십 해제
    ("UPDATE public.tenant_memberships m SET status = 'removed' WHERE m.user_id = $1", ["u_cross"]),
    ("DELETE FROM tenant_memberships WHERE user_id = 'u_e2e'", None),
    # 로그인 조직 끊김: 교차 계정의 실제 소속 T_B, e2e 계정 소속 T_A
    ("UPDATE public.tenants t SET status = 'suspended' WHERE t.id = $1::uuid", [T_B]),
    ("UPDATE tenants SET deleted_at = NOW() WHERE id = $1::uuid", [T_A]),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("sql,params", BLOCKED_CASES)
async def test_protected_write_blocked_dry_and_real(vault_key, sql, params):
    async with environment(vault_key) as (pool, ex):
        dry, real, before = await both(ex, pool, sql, params)
        assert dry["blocked"] is True and real["blocked"] is True, (dry, real)
        assert await dump(pool) == before


@pytest.mark.asyncio
async def test_unreferenced_inactive_and_external_are_cleanable(vault_key):
    async with environment(vault_key) as (pool, ex):
        for uid in ("u_free", "u_inactive", "u_ext"):
            sql = "UPDATE public.saas_users AS u SET deleted_at = NOW(), status = 'deleted' WHERE u.id = $1"
            dry, real, before = await both(ex, pool, sql, [uid])
            assert dry["success"] is True and dry["dry_run"] is True, dry
            assert real["success"] is True and real["result"] == "UPDATE 1", real
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status, deleted_at FROM public.saas_users WHERE id = $1", uid)
            assert row["status"] == "deleted" and row["deleted_at"] is not None
        # 보호 계정이 없는 조직(T_C)은 비활성화할 수 있다.
        dry, real, _ = await both(
            ex, pool, "UPDATE public.tenants SET status = 'suspended' WHERE id = $1::uuid", [T_C])
        assert real["success"] is True, real
        # Vault 를 저장만 한 조직(T_A) 이라도 거기 소속 보호 계정(u_e2e)이 있으면 막힌다 — 위 케이스.
        # 보호 테이블과 무관한 쓰기는 가드를 타지 않는다.
        res = await ex._db_safe_write({"sql": "UPDATE misc_notes SET body = 'm' WHERE id = 1"})
        assert res["success"] is True and "vault_guard" not in res


@pytest.mark.asyncio
async def test_mixed_batch_rolls_back_entirely(vault_key):
    async with environment(vault_key) as (pool, ex):
        before_rows = await protected_rows(pool)
        sql = ("UPDATE public.saas_users SET deleted_at = NOW(), status = 'deleted'"
               " WHERE id = ANY($1::text[])")
        dry, real, before = await both(ex, pool, sql, [["u_cross", "u_free2"]])
        assert dry["blocked"] is True and real["blocked"] is True
        after = await dump(pool)
        assert after == before  # 정상 건(u_free2)도 롤백됨
        async with pool.acquire() as conn:
            free2 = await conn.fetchrow(
                "SELECT status, deleted_at FROM public.saas_users WHERE id = 'u_free2'")
        assert free2["status"] == "active" and free2["deleted_at"] is None
        after_rows = await protected_rows(pool)
        print("\n[혼합 일괄요청 u_cross+u_free2] blocked:", real["error"])
        show("보호대상 before", before_rows)
        show("보호대상 after ", after_rows)
        assert before_rows == after_rows


@pytest.mark.asyncio
async def test_decrypt_failure_fails_closed(vault_key):
    async with environment(vault_key) as (pool, ex):
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO public.e2e_credentials(service, label, login_url, username_enc, password_enc)"
                " VALUES ('aads-dashboard', 'corrupt', $1, 'not-a-fernet-token', 'x')", INTERNAL)
        dry, real, before = await both(
            ex, pool, "UPDATE public.saas_users SET deleted_at = NOW() WHERE id = 'u_free'")
        assert dry["blocked"] is True and real["blocked"] is True
        assert "복호화 실패" in real["error"] and "not-a-fernet-token" not in str(real)
        assert await dump(pool) == before
        # 외부 origin 의 깨진 행은 판정에 쓰지 않으므로 막지 않는다.
        async with pool.acquire() as conn:
            await conn.execute("UPDATE public.e2e_credentials SET login_url = $1"
                               " WHERE label = 'corrupt'", EXTERNAL)
        res = await ex._db_safe_write(
            {"sql": "UPDATE public.saas_users SET deleted_at = NOW() WHERE id = 'u_free'"})
        assert res["success"] is True, res


@pytest.mark.asyncio
async def test_credential_deactivation_releases_protection(vault_key):
    async with environment(vault_key) as (pool, ex):
        async with pool.acquire() as conn:
            await conn.execute("UPDATE public.agent_vault_credentials SET is_active = false"
                               " WHERE origin = $1", INTERNAL)
        res = await ex._db_safe_write(
            {"sql": "UPDATE public.saas_users SET deleted_at = NOW() WHERE id = 'u_cross'"})
        assert res["success"] is True, res


P1B_SQL = [
    "UPDATE public.saas_users SET deleted_at = NOW() WHERE id = 'u_free'; COMMIT; BEGIN",
    "UPDATE public.saas_users SET deleted_at = NOW() WHERE id = 'u_cross'; COMMIT; BEGIN",
    "UPDATE misc_notes SET body = 'x' WHERE id = 1; COMMIT",
    "UPDATE public.saas_users SET status = 'deleted' WHERE id = 'u_e2e';"
    " DO $$BEGIN PERFORM 1; END$$",
]


@pytest.mark.asyncio
async def test_p1b_multi_statement_rejected_with_zero_changes(vault_key):
    async with environment(vault_key) as (pool, ex):
        # 대조군: 가드 없이 asyncpg 로 보내면 COMMIT 이 트랜잭션을 깨고 변경이 남는다.
        async with pool.acquire() as conn:
            tr = conn.transaction()
            await tr.start()
            await conn.execute(
                "UPDATE public.saas_users SET deleted_at = NOW() WHERE id = 'u_ctrl'; COMMIT; BEGIN")
            await tr.rollback()
            leaked = await conn.fetchval(
                "SELECT deleted_at IS NOT NULL FROM public.saas_users WHERE id = 'u_ctrl'")
        print("\n[P1-B 대조군] 가드 없는 asyncpg simple query + 외부 ROLLBACK 후 u_ctrl 삭제 잔존:", leaked)
        assert leaked is True

        # 2차 방어선: prepare 는 서버가 다중 문장을 거부한다.
        from app.services.tool_executor import _execute_single_statement
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresSyntaxError):
                await _execute_single_statement(conn, P1B_SQL[0], [])

        print("[P1-B 회귀] 실행 전후 행 수 대조")
        print("   mode    | sql# | alive_before | alive_after | notes_body_before→after | blocked")
        for idx, sql in enumerate(P1B_SQL):
            for dry_run in (True, False):
                before = await dump(pool)
                async with pool.acquire() as conn:
                    alive_before = await conn.fetchval(
                        "SELECT count(*) FROM public.saas_users WHERE deleted_at IS NULL"
                        " AND status = 'active'")
                with patch("app.core.db_pool.get_pool",
                           side_effect=AssertionError("P1-B 는 연결 전에 거부해야 한다")):
                    res = await ex._db_safe_write({"sql": sql, "dry_run": dry_run})
                async with pool.acquire() as conn:
                    alive_after = await conn.fetchval(
                        "SELECT count(*) FROM public.saas_users WHERE deleted_at IS NULL"
                        " AND status = 'active'")
                after = await dump(pool)
                print(f"   {'dry_run' if dry_run else 'execute':7} | {idx}    | {alive_before:12} |"
                      f" {alive_after:11} | {before['notes'][0][1]}→{after['notes'][0][1]}"
                      f"                     | {res.get('blocked')}")
                assert res["blocked"] is True and "error" in res, res
                assert alive_before == alive_after
                assert before == after
