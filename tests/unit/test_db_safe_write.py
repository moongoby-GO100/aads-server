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
