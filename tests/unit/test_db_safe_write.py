"""db_safe_write 도구 단위 테스트."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.tool_executor import ToolExecutor


class _FakeRow:
    def __init__(self, data):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeConn:
    def __init__(self, pre_count=10, execute_result="UPDATE 1"):
        self._pre_count = pre_count
        self._execute_result = execute_result
        self.execute_calls = []

    async def fetchrow(self, query, *args):
        return _FakeRow({"cnt": self._pre_count})

    def transaction(self):
        return _FakeTransaction()

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return self._execute_result

    async def prepare(self, query):
        # db_safe_write 는 다중 문장 차단을 위해 확장 프로토콜(prepare)로 실행한다.
        return _FakeStatement(self, query)


class _FakeStatement:
    def __init__(self, conn, query):
        self._conn = conn
        self._query = query

    async def fetch(self, *args):
        self._conn.execute_calls.append((self._query, args))
        return []

    def get_statusmsg(self):
        return self._conn._execute_result


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


@pytest.mark.asyncio
async def test_db_safe_write_blocks_drop():
    result = await ToolExecutor()._db_safe_write({"sql": "DROP TABLE chat_messages"})
    assert result == {"error": "차단된 명령: DROP"}


@pytest.mark.asyncio
async def test_db_safe_write_blocks_truncate():
    result = await ToolExecutor()._db_safe_write({"sql": "TRUNCATE users"})
    assert result == {"error": "차단된 명령: TRUNCATE"}


@pytest.mark.asyncio
async def test_db_safe_write_blocks_alter():
    result = await ToolExecutor()._db_safe_write({"sql": "ALTER TABLE users ADD COLUMN x int"})
    assert result == {"error": "차단된 명령: ALTER"}


@pytest.mark.asyncio
async def test_db_safe_write_rejects_select():
    result = await ToolExecutor()._db_safe_write({"sql": "SELECT * FROM chat_messages"})
    assert result == {"error": "INSERT/UPDATE/DELETE만 허용"}


@pytest.mark.asyncio
async def test_db_safe_write_rejects_empty_sql():
    result = await ToolExecutor()._db_safe_write({"sql": ""})
    assert result == {"error": "sql 파라미터 필수"}


@pytest.mark.asyncio
async def test_db_safe_write_dry_run_returns_without_executing():
    conn = _FakeConn(pre_count=42)

    with patch("app.core.db_pool.get_pool", return_value=_FakePool(conn)):
        result = await ToolExecutor()._db_safe_write({
            "sql": "UPDATE users SET name = 'Alice' WHERE id = 1",
            "dry_run": True,
        })

    assert result["dry_run"] is True
    assert result["table"] == "users"
    assert result["pre_count"] == 42
    assert "dry_run" in result["message"]
    assert conn.execute_calls == []


@pytest.mark.asyncio
async def test_db_safe_write_execute_returns_counts():
    conn = _FakeConn(pre_count=10, execute_result="UPDATE 1")

    with patch("app.core.db_pool.get_pool", return_value=_FakePool(conn)):
        result = await ToolExecutor()._db_safe_write({
            "sql": "UPDATE users SET active = true WHERE id = 1",
            "params": [],
            "dry_run": False,
        })

    assert result["success"] is True
    assert result["table"] == "users"
    assert result["pre_count"] == 10
    assert result["post_count"] == 10
    assert len(conn.execute_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_db_safe_write_rejects_multi_statement_before_connecting(dry_run):
    # P1-B: 다중 문장·트랜잭션 제어는 DB 연결 전에 거부한다(실행·dry_run 동일).
    def _no_pool():
        raise AssertionError("pool must not be acquired")

    with patch("app.core.db_pool.get_pool", side_effect=_no_pool):
        result = await ToolExecutor()._db_safe_write({
            "sql": "UPDATE public.saas_users SET deleted_at=NOW() WHERE id='x'; COMMIT; BEGIN",
            "dry_run": dry_run,
        })
    assert result["blocked"] is True
    assert result["dry_run"] is dry_run


def _class_db_safe_write_defs():
    import ast
    import inspect

    from app.services import tool_executor as te

    tree = ast.parse(inspect.getsource(te))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ToolExecutor")
    return [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_db_safe_write"]


def test_db_safe_write_effective_definition_is_last_and_guarded():
    # 클래스 본문의 마지막 정의가 효력을 갖는다. 앞 정의는 보존용(도달 불가)으로 남는다.
    import inspect

    defs = _class_db_safe_write_defs()
    assert len(defs) >= 2, "앞 정의(보존용)가 삭제됨"
    last = defs[-1]
    assert ToolExecutor._db_safe_write.__code__.co_firstlineno == last.lineno

    body = inspect.getsource(ToolExecutor._db_safe_write)
    assert "validate_single_write" in body
    assert "references_login_tables" in body
    assert "_db_safe_write_vault_guarded" in body
    for earlier in defs[:-1]:
        assert earlier.lineno != last.lineno


def test_db_safe_write_preserved_symbols_still_defined():
    from app.services import tool_executor as te

    assert issubclass(te._DryRunRollback, Exception)
    assert issubclass(te._MaxAffectedExceeded, Exception)
    assert "DROP" in ToolExecutor._DB_SAFE_BLOCKED_KEYWORDS


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_dispatch_db_safe_write_binds_guarded_implementation(dry_run):
    # 도구 등록(_dispatch 맵)의 db_safe_write 가 가드 구현을 탄다:
    # 다중 문장 SQL 은 get_pool() 호출 전에 거부되어야 한다.
    def _no_pool():
        raise AssertionError("pool must not be acquired")

    with patch("app.core.db_pool.get_pool", side_effect=_no_pool):
        result = await ToolExecutor()._dispatch("db_safe_write", {
            "sql": "DELETE FROM public.tenants WHERE id='x'; DELETE FROM public.saas_users",
            "dry_run": dry_run,
        })
    assert result["blocked"] is True
    assert result["dry_run"] is dry_run
