"""Vault 참조 로그인 보호(P1-A) + db_safe_write 단일 문장 선차단(P1-B) 단위 테스트.

DB 없이 판정 로직만 본다. 실제 PostgreSQL 동작은
tests/integration/test_vault_cleanup_guard_postgres.py 가 검증한다.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.db_write_sql_guard import SqlGuardError, validate_single_write
from app.services.tool_executor import ToolExecutor
from app.services.vault_cleanup_guard import (
    LoginSnapshot,
    VaultGuardUnavailable,
    VaultLoginProtected,
    assert_login_preserved,
    is_internal_origin,
    normalize_email,
    protected_login_snapshot,
    references_login_tables,
)

# ── P1-B: 단일 문장 렉서 ─────────────────────────────────────────────────────


@pytest.mark.parametrize("sql", [
    "UPDATE public.saas_users SET deleted_at=NOW() WHERE id='x'; COMMIT; BEGIN",
    "UPDATE t SET a=1; COMMIT",
    "UPDATE t SET a=1;COMMIT;",
    "UPDATE t SET a=1; ROLLBACK",
    "UPDATE t SET a=1; SAVEPOINT s",
    "UPDATE t SET a=1; RELEASE SAVEPOINT s",
    "UPDATE t SET a=1; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
    "UPDATE t SET a=1; DO $$BEGIN PERFORM 1; END$$",
    "UPDATE t SET a=1; CALL p()",
    "UPDATE t SET a=1; END",
    "UPDATE t SET a='x'; DELETE FROM t",
    "UPDATE t SET a=$$;$$; DELETE FROM t",
    "UPDATE t SET a=1 /* c */; COMMIT",
    "UPDATE t SET a=1 -- c\n; COMMIT",
    "UPDATE t SET a=E'\\''; COMMIT; --'",
])
def test_multi_statement_rejected(sql):
    with pytest.raises(SqlGuardError):
        validate_single_write(sql)


@pytest.mark.parametrize("sql", [
    "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT a", "RELEASE a", "SET TRANSACTION READ WRITE",
    "START TRANSACTION", "DO $$BEGIN END$$", "CALL p()", "SELECT 1", "WITH x AS (SELECT 1) DELETE FROM t",
])
def test_non_write_statement_rejected(sql):
    with pytest.raises(SqlGuardError):
        validate_single_write(sql)


@pytest.mark.parametrize("sql", [
    "UPDATE t SET a='x;y' WHERE b=1",
    "UPDATE t SET a='it''s; ok'",
    'UPDATE "we;ird" SET a=1',
    "UPDATE t SET a=$$ ; COMMIT; $$",
    "UPDATE t SET a=$tag$ ; $$ ; $tag$",
    "UPDATE t SET a=E'\\'; still string'",
    "UPDATE t SET a=1 /* ; COMMIT; /* nested ; */ ; */",
    "UPDATE t SET a=1 -- ; COMMIT",
    "UPDATE t SET a=$1 WHERE b=$2;",
    "UPDATE t SET a=1;  -- trailing\n",
    "INSERT INTO t(a) VALUES ('BEGIN; COMMIT')",
    "DELETE FROM t WHERE a = 'x'",
])
def test_single_statement_accepted(sql):
    info = validate_single_write(sql)
    assert info.keyword in {"insert", "update", "delete"}


@pytest.mark.parametrize("sql", [
    "UPDATE t SET a='x", "UPDATE t SET a=$$x", "UPDATE t SET a=1 /* x", 'UPDATE "t SET a=1',
    "UPDATE U&\"saas\\0075sers\" SET status='deleted'", "UPDATE t SET a=U&'\\0041'",
])
def test_ambiguous_sql_fails_closed(sql):
    with pytest.raises(SqlGuardError):
        validate_single_write(sql)


def test_protected_table_detection_includes_quoted_and_qualified():
    for sql in (
        "UPDATE public.saas_users AS u SET deleted_at=NOW()",
        'UPDATE "public"."SAAS_USERS" SET status=\'deleted\'',
        "DELETE FROM tenant_memberships m WHERE m.user_id='a'",
        "UPDATE tenants t SET status='archived'",
        "UPDATE goals SET note = format('%I', 'saas_users')",
    ):
        info = validate_single_write(sql)
        assert references_login_tables(info.words + info.literals), sql
    info = validate_single_write("UPDATE goals SET title='x'")
    assert not references_login_tables(info.words + info.literals)


# ── P1-A: origin / 이메일 ─────────────────────────────────────────────────────


def test_internal_origin_only():
    assert is_internal_origin("https://aads.newtalk.kr/login")
    assert is_internal_origin("aads.newtalk.kr")
    assert is_internal_origin("https://AADS.newtalk.kr./chat")
    assert not is_internal_origin("https://aads.newtalk.kr.evil.example/login")
    assert not is_internal_origin("https://aads.newtalk.kr@evil.example/login")
    assert not is_internal_origin("https://go100.newtalk.kr/login")
    assert not is_internal_origin("javascript://aads.newtalk.kr")
    assert not is_internal_origin("")
    assert not is_internal_origin(None)


def test_internal_hosts_env(monkeypatch):
    monkeypatch.setenv("AADS_VAULT_GUARD_INTERNAL_HOSTS", "obys.example.kr, other.example")
    assert is_internal_origin("https://obys.example.kr/login")
    assert is_internal_origin("https://other.example/")


def test_normalize_email():
    assert normalize_email("  User@Example.COM \n") == "user@example.com"


class FakeConn:
    """protected_login_snapshot 이 보내는 쿼리만 흉내 낸다."""

    def __init__(self, credentials, users, memberships, tenants, fail_on=None):
        self.credentials = credentials
        self.users = users
        self.memberships = memberships
        self.tenants = tenants
        self.fail_on = fail_on
        self.sql_params: list = []

    async def execute(self, sql, *args):
        assert "LOCK TABLE" in sql

    async def fetch(self, sql, *args):
        self.sql_params.append(args)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")
        if "UNION ALL" in sql:
            return self.credentials
        if "to_jsonb(u)" in sql:
            return [{"id": u["id"], "row": u} for u in self.users if u["id"] in args[0]]
        if "FROM public.saas_users" in sql:
            return [{"id": u["id"], "email": u["email"]} for u in self.users]
        if "FROM public.tenant_memberships" in sql:
            return [{"user_id": m["user_id"], "tenant_id": m["tenant_id"], "row": m}
                    for m in self.memberships if m["user_id"] in args[0]]
        if "FROM public.tenants" in sql:
            return [{"id": t["id"], "row": t} for t in self.tenants if t["id"] in args[0]]
        raise AssertionError(sql)


def _user(uid, email, tenant):
    return {"id": uid, "email": email, "default_tenant_id": tenant, "status": "active",
            "is_active": True, "deleted_at": None}


@pytest.fixture
def conn():
    credentials = [
        # 조직 T1 의 Vault 에 조직 T2 계정(cross)이 저장돼 있다 — tenant 는 판정에 쓰지 않는다.
        {"login_origin": "https://aads.newtalk.kr/login", "service": None, "username_enc": "c-cross"},
        {"login_origin": None, "service": "aads-dashboard", "username_enc": "c-e2e"},
        {"login_origin": "https://external.example/login", "service": None, "username_enc": "c-ext"},
    ]
    users = [
        _user("u-cross", "cross@b.example", "T2"),
        _user("u-e2e", "E2E@A.example", "T1"),
        _user("u-ext", "ext@a.example", "T1"),
        _user("u-free", "free@a.example", "T1"),
    ]
    memberships = [
        {"user_id": "u-cross", "tenant_id": "T2", "status": "active", "deleted_at": None},
        {"user_id": "u-cross", "tenant_id": "T3", "status": "removed", "deleted_at": None},
        {"user_id": "u-free", "tenant_id": "T1", "status": "active", "deleted_at": None},
    ]
    tenants = [{"id": t, "status": "active", "deleted_at": None} for t in ("T1", "T2", "T3")]
    return FakeConn(credentials, users, memberships, tenants)


PLAIN = {"c-cross": " Cross@B.example ", "c-e2e": "e2e@a.example", "c-ext": "ext@a.example"}


@pytest.mark.asyncio
async def test_snapshot_matches_by_email_and_internal_origin_not_vault_tenant(conn):
    with patch("app.services.vault_cleanup_guard.decrypt_value", side_effect=PLAIN.__getitem__):
        snap = await protected_login_snapshot(conn)
    assert set(snap.users) == {"u-cross", "u-e2e"}           # 외부 origin(u-ext)·비참조(u-free) 제외
    assert set(snap.memberships) == {("u-cross", "T2")}      # removed 멤버십은 보호 대상 아님
    assert set(snap.tenants) == {"T1", "T2"}
    assert snap.reference_count == 2
    # 복호화한 평문은 SQL 파라미터로 나가지 않는다.
    flat = repr(conn.sql_params).lower()
    assert "@" not in flat


@pytest.mark.asyncio
async def test_decrypt_failure_fails_closed(conn):
    with patch("app.services.vault_cleanup_guard.decrypt_value",
               side_effect=ValueError("secret-cipher-text")):
        with pytest.raises(VaultGuardUnavailable) as err:
            await protected_login_snapshot(conn)
    assert "secret-cipher-text" not in str(err.value)


@pytest.mark.asyncio
async def test_lookup_failure_fails_closed(conn):
    conn.fail_on = "UNION ALL"
    with pytest.raises(VaultGuardUnavailable):
        await protected_login_snapshot(conn)


@pytest.mark.asyncio
async def test_external_decrypt_failure_is_not_needed(conn):
    # 외부 origin 행은 복호화하지 않는다 — 깨진 외부 자격증명이 보호 판정을 막지 않는다.
    plain = dict(PLAIN)
    del plain["c-ext"]
    with patch("app.services.vault_cleanup_guard.decrypt_value", side_effect=plain.__getitem__):
        snap = await protected_login_snapshot(conn)
    assert "u-ext" not in snap.users


def _snap():
    return LoginSnapshot(
        users={"u1": {"email": "a@x", "status": "active", "is_active": True,
                      "deleted_at": None, "default_tenant_id": "T1"}},
        memberships={("u1", "T1"): {"status": "active", "deleted_at": None}},
        tenants={"T1": {"status": "active", "deleted_at": None}},
    )


@pytest.mark.parametrize("mutate", [
    lambda s: s.users["u1"].update(deleted_at="2026-09-30"),
    lambda s: s.users["u1"].update(status="deleted"),
    lambda s: s.users["u1"].update(status="suspended"),
    lambda s: s.users["u1"].update(is_active=False),
    lambda s: s.users["u1"].update(email="b@x"),
    lambda s: s.users["u1"].update(default_tenant_id=None),
    lambda s: s.users.pop("u1"),
    lambda s: s.memberships[("u1", "T1")].update(status="removed"),
    lambda s: s.memberships[("u1", "T1")].update(deleted_at="2026-09-30"),
    lambda s: s.memberships.pop(("u1", "T1")),
    lambda s: s.tenants["T1"].update(status="archived"),
    lambda s: s.tenants["T1"].update(deleted_at="2026-09-30"),
    lambda s: s.tenants.pop("T1"),
])
def test_deactivation_blocked(mutate):
    before, after = _snap(), _snap()
    mutate(after)
    with pytest.raises(VaultLoginProtected):
        assert_login_preserved(before, after)


def test_unrelated_change_allowed():
    before, after = _snap(), _snap()
    after.users["u1"]["default_tenant_id"] = "T9"
    assert_login_preserved(before, after)


# ── db_safe_write 경로 ───────────────────────────────────────────────────────


class _Txn:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        self.log.append("begin")

    async def __aexit__(self, exc_type, *args):
        self.log.append("rollback" if exc_type else "commit")
        return False


class _Stmt:
    def __init__(self, log):
        self.log = log

    async def fetch(self, *args):
        self.log.append("write")
        return []

    def get_statusmsg(self):
        return "UPDATE 1"


class _GuardConn:
    def __init__(self):
        self.log: list[str] = []

    def transaction(self):
        return _Txn(self.log)

    async def execute(self, sql, *args):
        self.log.append("set")

    async def fetch(self, sql, *args):
        # 쓰기 대상 뷰 여부 조회 — 뷰 없음(트랜잭션 로그에 남기지 않는다).
        return []

    async def prepare(self, sql):
        return _Stmt(self.log)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *args):
                return False
        return _A()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_guarded_violation_rolls_back_whole_transaction(dry_run):
    conn = _GuardConn()
    before, after = _snap(), _snap()
    after.users["u1"]["deleted_at"] = "now"

    async def _before(c):
        return before

    async def _after(c, b):
        return after

    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)), \
            patch("app.services.vault_cleanup_guard.protected_login_snapshot", _before), \
            patch("app.services.vault_cleanup_guard.reload_snapshot", _after):
        result = await ToolExecutor()._db_safe_write({
            "sql": "UPDATE public.saas_users u SET deleted_at=NOW() WHERE u.id IN ('u1','u2')",
            "dry_run": dry_run,
        })
    assert result["blocked"] is True and result["success"] is False
    assert result["dry_run"] is dry_run
    assert conn.log[-1] == "rollback"
    assert "u1" in result["error"]


@pytest.mark.asyncio
async def test_guarded_dry_run_rolls_back_even_when_allowed():
    conn = _GuardConn()

    async def _snap_fn(c, *a):
        return _snap()

    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)), \
            patch("app.services.vault_cleanup_guard.protected_login_snapshot", _snap_fn), \
            patch("app.services.vault_cleanup_guard.reload_snapshot", _snap_fn):
        dry = await ToolExecutor()._db_safe_write({
            "sql": "UPDATE saas_users SET name='n' WHERE id='u1'", "dry_run": True})
        real = await ToolExecutor()._db_safe_write({
            "sql": "UPDATE saas_users SET name='n' WHERE id='u1'"})
    assert dry["success"] is True and dry["dry_run"] is True
    assert real["success"] is True and real["dry_run"] is False
    assert dry["vault_protected_accounts"] == real["vault_protected_accounts"] == 1
    assert conn.log == ["begin", "set", "write", "rollback", "begin", "set", "write", "commit"]


@pytest.mark.asyncio
async def test_guard_unavailable_blocks():
    conn = _GuardConn()

    async def _fail(c):
        raise VaultGuardUnavailable("Vault 로그인 참조 복호화 실패(InvalidToken) — 쓰기 차단")

    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)), \
            patch("app.services.vault_cleanup_guard.protected_login_snapshot", _fail):
        result = await ToolExecutor()._db_safe_write({
            "sql": "UPDATE tenants SET status='archived' WHERE id=$1::uuid", "params": ["x"]})
    assert result["blocked"] is True
    assert "write" not in conn.log
