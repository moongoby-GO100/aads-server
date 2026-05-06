from __future__ import annotations

from unittest.mock import patch

import pytest


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.last_query = ""
        self.last_args = ()

    async def fetch(self, query: str, *args):
        self.last_query = query
        self.last_args = args
        return self._rows


class _FakePool:
    def __init__(self, rows):
        self.conn = _FakeConn(rows)

    def acquire(self):
        return _FakeAcquire(self.conn)


@pytest.mark.asyncio
async def test_tool_metrics_aggregates_summary_and_top3():
    from app.services.tool_executor import ToolExecutor

    rows = [
        {
            "tool_name": "query_database",
            "total_calls": 20,
            "failed_calls": 2,
            "avg_latency_ms": 120.5,
            "p95_latency_ms": 300.0,
        },
        {
            "tool_name": "read_remote_file",
            "total_calls": 10,
            "failed_calls": 1,
            "avg_latency_ms": 80.0,
            "p95_latency_ms": 500.0,
        },
        {
            "tool_name": "web_search",
            "total_calls": 5,
            "failed_calls": 0,
            "avg_latency_ms": 220.0,
            "p95_latency_ms": 450.0,
        },
        {
            "tool_name": "slow_tool",
            "total_calls": 2,
            "failed_calls": 1,
            "avg_latency_ms": 1000.0,
            "p95_latency_ms": 1400.0,
        },
    ]
    fake_pool = _FakePool(rows)
    executor = ToolExecutor()

    with patch("app.core.db_pool.get_pool", return_value=fake_pool):
        result = await executor._tool_metrics({"period": "7d"})

    assert result["period"] == "7d"
    assert result["summary"]["total_calls"] == 37
    assert result["summary"]["failed_calls"] == 4
    assert result["summary"]["failure_rate_pct"] == 10.81
    assert result["summary"]["slowest_tools_top3"][0]["tool_name"] == "slow_tool"
    assert result["summary"]["slowest_tools_top3"][1]["tool_name"] == "read_remote_file"
    assert result["summary"]["slowest_tools_top3"][2]["tool_name"] == "web_search"
    assert fake_pool.conn.last_args == ("7 days", "")
    assert "COUNT(*) FILTER (WHERE success = false)" in fake_pool.conn.last_query
    assert "PERCENTILE_CONT(0.95)" in fake_pool.conn.last_query
    assert "$1::interval" in fake_pool.conn.last_query


@pytest.mark.asyncio
async def test_tool_metrics_uses_bind_parameters_for_tool_name_filter():
    from app.services.tool_executor import ToolExecutor

    fake_pool = _FakePool([])
    executor = ToolExecutor()
    payload = "read_remote_file'; DROP TABLE tool_results_archive; --"

    with patch("app.core.db_pool.get_pool", return_value=fake_pool):
        result = await executor._tool_metrics({"tool_name": payload})

    assert result["period"] == "24h"
    assert fake_pool.conn.last_args == ("24 hours", payload)
    assert payload not in fake_pool.conn.last_query


@pytest.mark.asyncio
async def test_tool_metrics_rejects_invalid_period_without_db_call():
    from app.services.tool_executor import ToolExecutor

    executor = ToolExecutor()
    with patch("app.core.db_pool.get_pool", side_effect=AssertionError("DB should not be called")):
        result = await executor._tool_metrics({"period": "90d"})

    assert result == {"error": "period must be one of: 24h, 7d, 30d"}
