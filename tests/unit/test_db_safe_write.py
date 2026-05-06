from __future__ import annotations

from unittest.mock import patch

import pytest


class _FakeTransaction:
    def __init__(self) -> None:
        self.started = False
        self.committed = False
        self.rolled_back = False

    async def start(self) -> None:
        self.started = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeAcquire:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    async def __aenter__(self) -> "_FakeConn":
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: "_FakeConn") -> None:
        self.conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)


class _FakeConn:
    def __init__(
        self,
        *,
        fetchval_results: list[int] | None = None,
        fetch_rows: list[dict[str, str]] | None = None,
        execute_result: str = "UPDATE 1",
    ) -> None:
        self.tx = _FakeTransaction()
        self.fetchval_results = list(fetchval_results or [])
        self.fetch_rows = fetch_rows or [{"QUERY PLAN": "Update on users  (cost=0.00..1.01 rows=1 width=0)"}]
        self.execute_result = execute_result
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> _FakeTransaction:
        return self.tx

    async def fetchval(self, query: str, *args):
        self.fetchval_calls.append((query, args))
        if not self.fetchval_results:
            raise AssertionError(f"unexpected fetchval query: {query}")
        return self.fetchval_results.pop(0)

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        return self.fetch_rows

    async def execute(self, query: str, *args):
        self.execute_calls.append((query, args))
        return self.execute_result


@pytest.mark.asyncio
async def test_db_safe_write_dry_run_rolls_back_and_returns_explain():
    from app.services.tool_executor import ToolExecutor

    conn = _FakeConn(fetchval_results=[1])
    executor = ToolExecutor()

    with patch("app.core.db_pool.get_pool", return_value=_FakePool(conn)):
        result = await executor._db_safe_write(
            {
                "sql": "UPDATE users SET name = $1 WHERE id = $2",
                "params": ["Alice", 42],
                "dry_run": True,
                "max_affected": 10,
            }
        )

    assert result["mode"] == "dry_run"
    assert result["estimated_rows"] == 1
    assert result["explain_plan"]
    assert conn.tx.started is True
    assert conn.tx.rolled_back is True
    assert conn.tx.committed is False
    assert conn.execute_calls == []
    assert conn.fetchval_calls[0][0] == "SELECT COUNT(*) FROM users WHERE id = $1"
    assert conn.fetchval_calls[0][1] == (42,)
    assert conn.fetch_calls[0][0].startswith("EXPLAIN UPDATE users")


@pytest.mark.asyncio
async def test_db_safe_write_max_affected_exceeded_rolls_back():
    from app.services.tool_executor import ToolExecutor

    conn = _FakeConn(fetchval_results=[101])
    executor = ToolExecutor()

    with patch("app.core.db_pool.get_pool", return_value=_FakePool(conn)):
        result = await executor._db_safe_write(
            {
                "sql": "DELETE FROM users WHERE disabled = $1",
                "params": [True],
                "dry_run": False,
                "max_affected": 100,
            }
        )

    assert "영향 행 수 101이 제한 100 초과" in result["error"]
    assert result["rolled_back"] is True
    assert result["success"] is False
    assert conn.tx.started is True
    assert conn.tx.rolled_back is True
    assert conn.tx.committed is False
    assert conn.fetch_calls == []
    assert conn.execute_calls == []


@pytest.mark.asyncio
async def test_db_safe_write_blocks_dangerous_ddl():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._db_safe_write({"sql": "DROP TABLE chat_messages"})

    assert result == {"error": "위험한 DDL은 차단됩니다"}


@pytest.mark.asyncio
async def test_db_safe_write_rejects_select_only_query():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._db_safe_write({"sql": "SELECT * FROM chat_messages"})

    assert result == {"error": "읽기 전용 쿼리는 query_db를 사용하세요"}
