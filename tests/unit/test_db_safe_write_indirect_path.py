"""db_safe_write 간접 쓰기 경로 차단 단위 테스트 (2026-09-30).

보호 테이블 이름이 SQL 에 나타나지 않는 경로 — 저장 함수·프로시저·뷰·확정 못 한 대상 —
를 fail closed 로 막는다. DB 없이 판정 로직과 실행 순서만 본다.
SQL 은 조각으로 조립한다(완전한 문장 리터럴을 한 줄로 두지 않는다 — scripts/dup_guard.py 오탐 경로).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import db_write_sql_guard as guard
from app.services.db_write_sql_guard import (
    ALLOWED_BUILTIN_FUNCTIONS,
    PROCEDURAL_KEYWORDS,
    TRANSACTION_CONTROL_KEYWORDS,
    IndirectWriteBlocked,
    SqlGuardError,
    WriteTarget,
    validate_single_write,
)
from app.services.tool_executor import ToolExecutor
from app.services.vault_cleanup_guard import (
    LoginSnapshot,
    VaultGuardUnavailable,
    VaultViewTargetBlocked,
    assert_target_not_view,
    target_unresolved,
)


def _sql(*parts: str) -> str:
    return " ".join(parts)


# ── 판정 로직 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sql", [
    _sql("SELECT", "some_fn(1)"),
    _sql("SELECT", "public.some_fn(1)"),
    _sql("CALL", "proc()"),
    "DO $$ BEGIN PERFORM some_fn(1); END $$",
    _sql("EXECUTE", "stmt_name"),
    _sql("UPDATE", "t", "SET", "a", "=", "my_fn(1)"),
    _sql("UPDATE", "t", "SET", "a", "=", "public.lower(x)"),
    _sql("UPDATE", "t", "SET", "a", "=", '"lower"(x)'),
    _sql("UPDATE", "t", "SET", "a", "=", '"MY_FN"(x)'),
    _sql("UPDATE", "t", "SET", "a", "=", "my_fn /* c */ (1)"),
    _sql("UPDATE", "t", "SET", "a", "=", "(SELECT my_fn(1))"),
    _sql("INSERT INTO", "t", "(a)", "VALUES", "(my_fn(1))"),
    _sql("DELETE FROM", "t", "WHERE", "a", "=", "query_to_xml('x', true, true, '')"),
    _sql("UPDATE", "t", "SET", "a", "=", "set_config('k', 'v', false)"),
    _sql("UPDATE", "t", "SET", "a", "=", "nextval('s')"),
    _sql("UPDATE", "t", "SET", "a", "=", "1", "WHERE", "b", "OPERATOR(pg_catalog.=)", "2"),
])
def test_indirect_call_rejected(sql):
    with pytest.raises(IndirectWriteBlocked):
        validate_single_write(sql)


def test_indirect_block_is_a_sql_guard_error():
    assert issubclass(IndirectWriteBlocked, SqlGuardError)


@pytest.mark.parametrize("sql", [
    _sql("UPDATE", "t", "SET", "updated_at", "=", "now()", "WHERE", "id", "=", "$1"),
    _sql("UPDATE", "t", "SET", "n", "=", "coalesce(n, 0) + 1", "WHERE", "id", "=", "$1"),
    _sql("UPDATE", "t", "SET", "name", "=", "lower(trim($1))", "WHERE", "id", "=", "$2"),
    _sql("UPDATE", "t", "SET", "data", "=", "jsonb_set(data, '{a}', to_jsonb($1::int))"),
    _sql("UPDATE", "t", "SET", "a", "=", "pg_catalog.lower(x)"),
    _sql("UPDATE", "t", "SET", "a", "=", "now ( )"),
    _sql("UPDATE", "t", "SET", "a", "=", "x::varchar(20)", ",", "b", "=", "CAST(y AS numeric(10,2))"),
    _sql("UPDATE", "t", "SET", "a", "=", "1.5", "WHERE", "b", "IN", "(SELECT id FROM x WHERE n > 2)"),
    _sql("UPDATE", "t", "SET", "a", "=", "1", "WHERE", "id", "=", "ANY($1::int[])"),
    _sql("UPDATE", "t", "SET", "n", "=", "(SELECT count(*) FROM x)"),
    _sql("UPDATE", "t", "SET", "n", "=", "n + (2 * 3)", "WHERE", "a", "=", "(SELECT max(a) FROM x)"),
    _sql("UPDATE", "t", "SET", "a", "=", "$1", "WHERE", "(a, b)", "OVERLAPS", "(c, d)"),
    _sql("INSERT INTO", "public.t", "(a, b)", "VALUES", "($1, now())",
         "ON CONFLICT", "(a)", "DO UPDATE SET", "b", "=", "EXCLUDED.b"),
    _sql("INSERT INTO", "t", "AS x", "(a)", "SELECT", "*", "FROM", "(VALUES (1))", "v (a)"),
    _sql("INSERT INTO", "t", "(id)", "VALUES", "(gen_random_uuid())"),
    _sql("DELETE FROM", "t", "WHERE", "created_at", "<", "now() - interval '1 day'"),
    _sql("DELETE FROM", "t", "WHERE", "NOT EXISTS", "(SELECT 1 FROM x WHERE x.id = t.id)"),
    _sql("UPDATE", "goals", "SET", "note", "=", "format('%I', 'x')"),
])
def test_builtin_function_not_false_positive(sql):
    info = validate_single_write(sql)
    assert info.keyword in {"insert", "update", "delete"}


def test_string_or_comment_named_like_a_call_is_not_a_call():
    info = validate_single_write(_sql("UPDATE", "t", "SET", "a", "=", "'my_fn(1)'", "-- other_fn(2)"))
    assert info.target == WriteTarget(None, "t")
    validate_single_write("UPDATE t SET a = $$ x_fn(1) $$ /* y_fn(2) */")


@pytest.mark.parametrize("sql,expected", [
    (_sql("UPDATE", "t", "SET", "a", "=", "1"), WriteTarget(None, "t")),
    (_sql("UPDATE", "ONLY", "public.t", "*", "SET", "a", "=", "1"), WriteTarget("public", "t")),
    (_sql("UPDATE", '"Public"."My T"', "AS", "u", "SET", "a", "=", "1"), WriteTarget("public", "my t")),
    (_sql("DELETE FROM", "reports.v_users", "WHERE", "a", "=", "1"), WriteTarget("reports", "v_users")),
    (_sql("INSERT INTO", "t", "(a)", "VALUES", "(1)"), WriteTarget(None, "t")),
    (_sql("INSERT INTO", "s.t", "DEFAULT VALUES"), WriteTarget("s", "t")),
])
def test_target_resolved(sql, expected):
    assert validate_single_write(sql).target == expected


@pytest.mark.parametrize("sql", [
    _sql("UPDATE", "db.public.t", "SET", "a", "=", "1"),
    _sql("DELETE FROM", "db.public.t", "WHERE", "a", "=", "1"),
    _sql("INSERT INTO", "db.public.t", "(a)", "VALUES", "(1)"),
    _sql("UPDATE", '"db"."public"."t"', "SET", "a", "=", "1"),
])
def test_unresolvable_target_is_none(sql):
    info = validate_single_write(sql)
    assert info.target is None
    assert target_unresolved(info.target)


def test_number_dot_is_not_a_name_separator():
    tokens: list = []
    guard._split_statements("UPDATE t SET a = 1.5, b = 2 WHERE c = x.y", tokens)
    assert ("n", "1.5") in tokens
    assert tokens.count(("p", ".")) == 1


def test_whitelist_excludes_side_effect_functions():
    for fn in ("set_config", "pg_sleep", "query_to_xml", "nextval", "setval", "dblink", "lo_import",
               "pg_terminate_backend", "pg_advisory_lock", "xpath"):
        assert fn not in ALLOWED_BUILTIN_FUNCTIONS
    for fn in ("now", "coalesce", "lower", "jsonb_set"):
        assert fn in ALLOWED_BUILTIN_FUNCTIONS


def test_guard_keyword_constants_only_grew():
    assert {"do", "call"} <= PROCEDURAL_KEYWORDS
    assert {"begin", "commit", "rollback", "savepoint", "release", "prepare", "set"} <= TRANSACTION_CONTROL_KEYWORDS


# ── db_safe_write 실행 순서 ──────────────────────────────────────────────────


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


class _Row:
    def __init__(self, cnt):
        self.cnt = cnt

    def __getitem__(self, key):
        return self.cnt


class _Conn:
    def __init__(self, views=(), fetch_error=None):
        self.log: list[str] = []
        self.views = list(views)
        self.fetch_error = fetch_error
        self.view_lookups: list[tuple] = []

    def transaction(self):
        return _Txn(self.log)

    async def execute(self, sql, *args):
        self.log.append("set")

    async def fetchrow(self, sql, *args):
        return _Row(7)

    async def fetch(self, sql, *args):
        self.view_lookups.append(args)
        if self.fetch_error:
            raise self.fetch_error
        return [{"hit": 1}] if self.views else []

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


def _no_pool():
    raise AssertionError("pool must not be acquired")


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("sql", [
    _sql("SELECT", "some_fn(1)"),
    _sql("CALL", "proc()"),
    "DO $$ BEGIN PERFORM some_fn(1); END $$",
    _sql("UPDATE", "t", "SET", "a", "=", "my_fn(1)"),
    _sql("UPDATE", "t", "SET", "a", "=", "public.my_fn(1)", "WHERE", "id", "=", "$1"),
])
async def test_indirect_call_rejected_before_pool(sql, dry_run):
    with patch("app.core.db_pool.get_pool", side_effect=_no_pool):
        result = await ToolExecutor()._db_safe_write({"sql": sql, "dry_run": dry_run})
    assert result["blocked"] is True
    assert result["dry_run"] is dry_run
    assert "차단" in result["error"]


@pytest.mark.asyncio
async def test_indirect_call_rejected_via_dispatch_before_pool():
    with patch("app.core.db_pool.get_pool", side_effect=_no_pool):
        result = await ToolExecutor()._dispatch("db_safe_write", {"sql": _sql("CALL", "proc()")})
    assert result["blocked"] is True


@pytest.mark.asyncio
async def test_builtin_function_write_is_executed():
    conn = _Conn()
    sql = _sql("UPDATE", "t", "SET", "updated_at", "=", "now()", "WHERE", "id", "=", "$1")
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)):
        result = await ToolExecutor()._db_safe_write({"sql": sql, "params": [1], "dry_run": False})
    assert result["success"] is True
    assert "write" in conn.log
    assert "set" not in conn.log  # 보호 경로(SET LOCAL)를 타지 않는다


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_view_target_rejected(dry_run):
    conn = _Conn(views=["v_accounts"])
    sql = _sql("UPDATE", "Reports.V_Accounts", "SET", "a", "=", "1")
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)):
        result = await ToolExecutor()._db_safe_write({"sql": sql, "dry_run": dry_run})
    assert result["blocked"] is True and result["dry_run"] is dry_run
    assert "뷰" in result["error"]
    assert conn.view_lookups == [("v_accounts", "reports")]
    assert conn.log == []  # 트랜잭션·쓰기 모두 시작 전


@pytest.mark.asyncio
async def test_view_lookup_failure_fails_closed():
    conn = _Conn(fetch_error=RuntimeError("boom"))
    sql = _sql("UPDATE", "t", "SET", "a", "=", "1")
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)):
        result = await ToolExecutor()._db_safe_write({"sql": sql})
    assert result["blocked"] is True
    assert "write" not in conn.log


@pytest.mark.asyncio
async def test_assert_target_not_view_lookup_contract():
    conn = _Conn()
    await assert_target_not_view(conn, None)
    assert conn.view_lookups == []
    await assert_target_not_view(conn, WriteTarget(None, "t"))
    assert conn.view_lookups == [("t", "")]
    with pytest.raises(VaultViewTargetBlocked):
        await assert_target_not_view(_Conn(views=["x"]), WriteTarget("public", "x"))
    with pytest.raises(VaultGuardUnavailable):
        await assert_target_not_view(_Conn(fetch_error=OSError("x")), WriteTarget(None, "t"))


def _snapshot_stubs(seen: list[str], after_deleted: bool = False):
    def _user(deleted):
        return {"email": "a@x", "status": "active", "is_active": True,
                "deleted_at": "now" if deleted else None, "default_tenant_id": None}

    async def _before(conn):
        seen.append("snapshot")
        return LoginSnapshot(users={"u1": _user(False)})

    async def _after(conn, before):
        seen.append("reload")
        return LoginSnapshot(users={"u1": _user(after_deleted)})

    return _before, _after


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [
    _sql("UPDATE", "db.public.accounts", "SET", "a", "=", "1"),
    _sql("DELETE FROM", "db.public.accounts", "WHERE", "a", "=", "1"),
])
async def test_unresolved_target_forces_protection_check(sql):
    conn, seen = _Conn(), []
    before, after = _snapshot_stubs(seen)
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)), \
            patch("app.services.vault_cleanup_guard.protected_login_snapshot", before), \
            patch("app.services.vault_cleanup_guard.reload_snapshot", after):
        result = await ToolExecutor()._db_safe_write({"sql": sql})
    assert result["vault_guard"] is True and result["success"] is True
    assert seen == ["snapshot", "reload"]
    assert conn.log == ["begin", "set", "write", "commit"]


@pytest.mark.asyncio
async def test_unresolved_target_that_kills_login_is_rolled_back():
    conn, seen = _Conn(), []
    before, after = _snapshot_stubs(seen, after_deleted=True)
    sql = _sql("UPDATE", "db.public.accounts", "SET", "a", "=", "1")
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)), \
            patch("app.services.vault_cleanup_guard.protected_login_snapshot", before), \
            patch("app.services.vault_cleanup_guard.reload_snapshot", after):
        result = await ToolExecutor()._db_safe_write({"sql": sql})
    assert result["blocked"] is True and result["success"] is False
    assert conn.log[-1] == "rollback"


@pytest.mark.asyncio
async def test_unresolved_target_guard_unavailable_blocks():
    conn = _Conn()

    async def _fail(c):
        raise VaultGuardUnavailable("조회 실패")

    sql = _sql("UPDATE", "db.public.accounts", "SET", "a", "=", "1")
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)), \
            patch("app.services.vault_cleanup_guard.protected_login_snapshot", _fail):
        result = await ToolExecutor()._db_safe_write({"sql": sql})
    assert result["blocked"] is True
    assert "write" not in conn.log


# ── 회귀: 보호 테이블 직접 쓰기 기존 동작 불변 ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [
    _sql("UPDATE", "public.saas_users", "SET", "deleted_at", "=", "now()", "WHERE", "id", "=", "$1"),
    _sql("DELETE FROM", "tenant_memberships", "WHERE", "user_id", "=", "$1"),
    _sql("UPDATE", "tenants", "SET", "status", "=", "'archived'"),
])
async def test_direct_protected_write_still_guarded(sql):
    conn, seen = _Conn(), []
    before, after = _snapshot_stubs(seen, after_deleted=True)
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)), \
            patch("app.services.vault_cleanup_guard.protected_login_snapshot", before), \
            patch("app.services.vault_cleanup_guard.reload_snapshot", after):
        result = await ToolExecutor()._db_safe_write({"sql": sql, "params": []})
    assert result["blocked"] is True and result["vault_guard"] is True
    assert seen == ["snapshot", "reload"]
    assert conn.log == ["begin", "set", "write", "rollback"]


@pytest.mark.asyncio
async def test_direct_protected_write_allowed_when_login_preserved():
    conn, seen = _Conn(), []
    before, after = _snapshot_stubs(seen, after_deleted=False)
    sql = _sql("UPDATE", "saas_users", "SET", "name", "=", "$1", "WHERE", "id", "=", "$2")
    with patch("app.core.db_pool.get_pool", return_value=_Pool(conn)), \
            patch("app.services.vault_cleanup_guard.protected_login_snapshot", before), \
            patch("app.services.vault_cleanup_guard.reload_snapshot", after):
        result = await ToolExecutor()._db_safe_write({"sql": sql, "params": ["n", "u1"]})
    assert result["success"] is True and result["vault_guard"] is True
    assert conn.log == ["begin", "set", "write", "commit"]


@pytest.mark.asyncio
async def test_multi_statement_still_rejected_before_pool():
    sql = "UPDATE saas_users SET a = 1; COMMIT"
    with patch("app.core.db_pool.get_pool", side_effect=_no_pool):
        result = await ToolExecutor()._db_safe_write({"sql": sql})
    assert result["blocked"] is True


@pytest.mark.asyncio
async def test_select_without_call_keeps_legacy_message():
    with patch("app.core.db_pool.get_pool", side_effect=_no_pool):
        result = await ToolExecutor()._db_safe_write({"sql": "SELECT * FROM chat_messages"})
    assert result == {"error": "INSERT/UPDATE/DELETE만 허용"}


# ── 운영 문서 ────────────────────────────────────────────────────────────────


def test_operations_doc_has_inspection_queries():
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "operations" / "DB_SAFE_WRITE_INDIRECT_PATH.md").read_text(encoding="utf-8")
    for needle in ("pg_trigger", "pg_proc", "saas_users", "tenant_memberships", "tenants", "오탐"):
        assert needle in doc
