from __future__ import annotations

from decimal import Decimal

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
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, query: str, *args):
        self.fetch_calls.append((query, args))
        return self._rows


class _FakePool:
    def __init__(self, rows):
        self.conn = _FakeConn(rows)

    def acquire(self):
        return _FakeAcquire(self.conn)


@pytest.mark.asyncio
async def test_tool_metrics_calculates_summary_and_slowest_top3():
    from app.services.tool_executor import ToolExecutor

    rows = [
        {
            "tool_name": "query_database",
            "call_count": 100,
            "fail_count": 10,
            "fail_rate_pct": Decimal("10.0"),
            "p95_latency_ms": Decimal("900"),
            "avg_latency_ms": Decimal("300"),
        },
        {
            "tool_name": "read_remote_file",
            "call_count": 50,
            "fail_count": 5,
            "fail_rate_pct": Decimal("10.0"),
            "p95_latency_ms": Decimal("1200"),
            "avg_latency_ms": Decimal("220"),
        },
        {
            "tool_name": "web_search",
            "call_count": 25,
            "fail_count": 0,
            "fail_rate_pct": Decimal("0.0"),
            "p95_latency_ms": Decimal("2500"),
            "avg_latency_ms": Decimal("1100"),
        },
        {
            "tool_name": "health_check",
            "call_count": 10,
            "fail_count": 1,
            "fail_rate_pct": Decimal("10.0"),
            "p95_latency_ms": Decimal("150"),
            "avg_latency_ms": Decimal("80"),
        },
    ]

    executor = ToolExecutor()
    executor._pool = _FakePool(rows)

    result = await executor._tool_metrics({"period": "24h"})

    assert result["period"] == "24h"
    assert result["tool_name"] is None
    assert len(result["metrics"]) == 4

    summary = result["summary"]
    assert summary["total_calls"] == 185
    assert summary["total_failures"] == 16
    assert summary["total_fail_rate_pct"] == 8.6

    slowest = summary["slowest_tools_top3"]
    assert [item["tool_name"] for item in slowest] == [
        "web_search",
        "read_remote_file",
        "query_database",
    ]

    fetch_query, fetch_args = executor._pool.conn.fetch_calls[0]
    assert "WHERE created_at > NOW() - $1::interval" in fetch_query
    assert "AND tool_name = $2" not in fetch_query
    assert fetch_args == ("24 hours",)


@pytest.mark.asyncio
async def test_tool_metrics_applies_tool_name_filter_with_bound_param():
    from app.services.tool_executor import ToolExecutor

    rows = [
        {
            "tool_name": "query_database",
            "call_count": 7,
            "fail_count": 1,
            "fail_rate_pct": Decimal("14.3"),
            "p95_latency_ms": Decimal("880"),
            "avg_latency_ms": Decimal("300"),
        }
    ]

    executor = ToolExecutor()
    executor._pool = _FakePool(rows)

    result = await executor._tool_metrics({"period": "7d", "tool_name": "query_database"})

    assert result["period"] == "7d"
    assert result["tool_name"] == "query_database"
    assert result["summary"]["total_calls"] == 7
    assert result["summary"]["total_failures"] == 1
    assert result["summary"]["total_fail_rate_pct"] == 14.3

    fetch_query, fetch_args = executor._pool.conn.fetch_calls[0]
    assert "AND tool_name = $2" in fetch_query
    assert fetch_args == ("7 days", "query_database")


@pytest.mark.asyncio
async def test_tool_metrics_rejects_invalid_period():
    from app.services.tool_executor import ToolExecutor

    executor = ToolExecutor()
    executor._pool = _FakePool([])

    result = await executor._tool_metrics({"period": "1h"})

    assert "error" in result
    assert "24h" in result["error"]
    assert executor._pool.conn.fetch_calls == []
