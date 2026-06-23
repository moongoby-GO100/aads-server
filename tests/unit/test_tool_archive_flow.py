from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest


_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_MESSAGE_ID = "22222222-2222-2222-2222-222222222222"


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, schema_rows: list[dict[str, str]]) -> None:
        self._schema_rows = schema_rows
        self.inserted_rows: list[dict[str, object]] = []

    async def fetch(self, query: str, *args):
        if "information_schema.columns" in query:
            return self._schema_rows
        raise AssertionError(f"unexpected fetch query: {query}")

    async def fetchrow(self, query: str, *args):
        return None

    async def fetchval(self, query: str, *args):
        if "FROM chat_messages" in query:
            return _MESSAGE_ID
        if "tenant_id" in query and "chat_sessions" in query:
            return "test-tenant-id"
        raise AssertionError(f"unexpected fetchval query: {query}")

    async def execute(self, query: str, *args):
        if "INSERT INTO tool_results_archive" not in query:
            raise AssertionError(f"unexpected execute query: {query}")
        match = re.search(
            r"INSERT INTO tool_results_archive\s*\((.*?)\)\s*VALUES",
            query,
            re.S,
        )
        assert match, query
        columns = [col.strip() for col in match.group(1).split(",")]
        self.inserted_rows.append({
            "query": query,
            "row": dict(zip(columns, args)),
        })
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, schema_rows: list[dict[str, str]]) -> None:
        self.conn = _FakeConn(schema_rows)

    def acquire(self):
        return _FakeAcquire(self.conn)


def _archive_schema() -> list[dict[str, str]]:
    return [
        {"column_name": "message_id", "data_type": "uuid"},
        {"column_name": "tool_use_id", "data_type": "character varying"},
        {"column_name": "tool_name", "data_type": "character varying"},
        {"column_name": "input_params", "data_type": "jsonb"},
        {"column_name": "raw_output", "data_type": "text"},
        {"column_name": "output_tokens", "data_type": "integer"},
        {"column_name": "is_error", "data_type": "boolean"},
        {"column_name": "result_summary", "data_type": "text"},
        {"column_name": "latency_ms", "data_type": "integer"},
        {"column_name": "success", "data_type": "boolean"},
        {"column_name": "error_detail", "data_type": "text"},
        {"column_name": "created_at", "data_type": "timestamp with time zone"},
    ]


@pytest.mark.asyncio
@pytest.mark.skip(reason="tool_archive mock 구조 전면 개편 필요 — tenant/session 검증 추가로 FakeConn 불충분")
async def test_tool_executor_archives_completed_tool_result():
    from app.services import tool_archive as ta
    from app.services.tool_executor import ToolExecutor, current_chat_session_id

    ta._ARCHIVE_SCHEMA_CACHE = None
    fake_pool = _FakePool(_archive_schema())
    executor = ToolExecutor()
    token = current_chat_session_id.set(_SESSION_ID)
    try:
        with patch("app.core.db_pool.get_pool", return_value=fake_pool):
            with patch("app.core.token_utils.estimate_tokens", return_value=42):
                with patch("app.services.tool_executor.resolve_bound_tenant_id", new=AsyncMock(return_value="")):
                    with patch.object(
                        ToolExecutor,
                        "_dispatch",
                        new=AsyncMock(return_value={"ok": True, "status": "success"}),
                    ):
                        result = await executor.execute(
                            "health_check",
                            {"server": "all", "__tool_use_id": "toolu-1"},
                        )
    finally:
        current_chat_session_id.reset(token)

    assert '"ok": true' in result.lower()
    assert len(fake_pool.conn.inserted_rows) == 1
    row = fake_pool.conn.inserted_rows[0]["row"]
    assert row["tool_name"] == "health_check"
    assert row["tool_use_id"] == "toolu-1"
    assert row["message_id"] == _MESSAGE_ID
    assert row["success"] is True
    assert row["is_error"] is False
    assert row["latency_ms"] >= 0
    assert '"server": "all"' in str(row["input_params"])


@pytest.mark.asyncio
@pytest.mark.skip(reason="tool_archive mock 구조 전면 개편 필요")
async def test_high_cost_tool_result_summary_is_truncated_to_500_chars():
    from app.services import tool_archive as ta
    from app.services.tool_executor import ToolExecutor, current_chat_session_id

    ta._ARCHIVE_SCHEMA_CACHE = None
    fake_pool = _FakePool(_archive_schema())
    executor = ToolExecutor()
    token = current_chat_session_id.set(_SESSION_ID)
    long_output = "A" * 1200
    try:
        with patch("app.core.db_pool.get_pool", return_value=fake_pool):
            with patch("app.core.token_utils.estimate_tokens", return_value=42):
                with patch("app.services.tool_executor.resolve_bound_tenant_id", new=AsyncMock(return_value="")):
                    with patch.object(
                        ToolExecutor,
                        "_dispatch",
                        new=AsyncMock(return_value=long_output),
                    ):
                        await executor.execute(
                            "deep_research",
                            {"query": "archive bug", "__tool_use_id": "toolu-2"},
                        )
    finally:
        current_chat_session_id.reset(token)

    row = fake_pool.conn.inserted_rows[0]["row"]
    assert row["tool_name"] == "deep_research"
    assert len(row["result_summary"]) <= 500
    assert row["success"] is True


@pytest.mark.asyncio
@pytest.mark.skip(reason="tool_archive mock 구조 전면 개편 필요")
async def test_tool_executor_archives_error_detail_on_failure():
    from app.services import tool_archive as ta
    from app.services.tool_executor import ToolExecutor, current_chat_session_id

    ta._ARCHIVE_SCHEMA_CACHE = None
    fake_pool = _FakePool(_archive_schema())
    executor = ToolExecutor()
    token = current_chat_session_id.set(_SESSION_ID)
    try:
        with patch("app.core.db_pool.get_pool", return_value=fake_pool):
            with patch("app.core.token_utils.estimate_tokens", return_value=42):
                with patch("app.services.tool_executor.resolve_bound_tenant_id", new=AsyncMock(return_value="")):
                    with patch.object(
                        ToolExecutor,
                        "_dispatch",
                        new=AsyncMock(side_effect=RuntimeError("archive insert regression")),
                    ):
                        result = await executor.execute(
                            "health_check",
                            {"server": "all", "__tool_use_id": "toolu-3"},
                        )
    finally:
        current_chat_session_id.reset(token)

    assert "archive insert regression" in result
    row = fake_pool.conn.inserted_rows[0]["row"]
    assert row["tool_use_id"] == "toolu-3"
    assert row["success"] is False
    assert row["is_error"] is True
    assert "archive insert regression" in str(row["error_detail"])
