from __future__ import annotations

import pytest


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _Conn:
    def __init__(self):
        self.calls = []

    async def fetch(self, query, interval_value, model):
        self.calls.append((query, interval_value, model))
        if "FROM chat_messages" in query:
            return [
                {
                    "model_key": "gpt-5.6-sol",
                    "calls": 3,
                    "failed_calls": 0,
                    "avg_latency_ms": 1200.4,
                    "p50_latency_ms": 1000.0,
                    "p95_latency_ms": 2200.0,
                    "max_latency_ms": 2400.0,
                }
            ]
        if "FROM pipeline_jobs" in query:
            return [
                {
                    "model_key": "codex:gpt-5.6-sol",
                    "calls": 2,
                    "failed_calls": 1,
                    "avg_latency_ms": 30000.0,
                    "p50_latency_ms": 28000.0,
                    "p95_latency_ms": 40000.0,
                    "max_latency_ms": 41000.0,
                }
            ]
        return []


@pytest.mark.asyncio
async def test_llm_response_metrics_aggregates_sources(monkeypatch):
    from app.services import llm_response_metrics

    conn = _Conn()
    monkeypatch.setattr(llm_response_metrics, "get_pool", lambda: _Pool(conn))

    result = await llm_response_metrics.get_llm_response_metrics(hours=7, model="gpt")

    assert result["period_hours"] == 7
    assert result["model_filter"] == "gpt"
    assert result["summary"]["total_observations"] == 5
    assert result["summary"]["failed_observations"] == 1
    assert result["summary"]["failure_rate_pct"] == 20.0
    assert result["summary"]["slowest_top5"][0]["source"] == "runner_cli_total"
    assert result["metrics"]["chat_final_response"][0]["provider"] == "openai"
    assert result["metrics"]["runner_cli_total"][0]["provider"] == "codex"
    assert {call[1].total_seconds() for call in conn.calls} == {25200.0}
    assert {call[2] for call in conn.calls} == {"gpt"}
