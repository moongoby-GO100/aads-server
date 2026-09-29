from __future__ import annotations

import asyncio

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
        if "GROUP BY event_type, model_key" in query:
            return [
                {
                    "event_type": "cli_first_stdout",
                    "model_key": "codex:gpt-5.6-sol",
                    "calls": 2,
                    "avg_duration_ms": 6400.0,
                    "p50_duration_ms": 6100.0,
                    "p95_duration_ms": 9000.0,
                    "max_duration_ms": 9400.0,
                }
            ]
        if "FROM pipeline_runner_events" in query:
            from datetime import datetime, timezone

            return [
                {
                    "job_id": "runner-test",
                    "project": "AADS",
                    "event_type": "cli_process_started",
                    "status": "running",
                    "phase": "claude_code_work",
                    "model_key": "codex:gpt-5.6-sol",
                    "size": "M",
                    "duration_ms": 0,
                    "observed_at": datetime(2026, 9, 4, tzinfo=timezone.utc),
                    "metadata": {"runner_kind": "codex_cli"},
                }
            ]
        if "FROM chat_messages" in query:
            # 표본 단위 — 실패 판정이 없는 소스라 failed 는 NULL
            return [
                {"model_key": "gpt-5.6-sol", "latency_ms": 1000.0, "failed": None},
                {"model_key": "gpt-5.6-sol", "latency_ms": 2400.0, "failed": None},
                {"model_key": "gpt-5.6-sol", "latency_ms": 200.0, "failed": None},
            ]
        if "FROM pipeline_jobs" in query:
            return [
                {"model_key": "codex:gpt-5.6-sol", "latency_ms": 41000.0, "failed": False},
                {"model_key": "codex:gpt-5.6-sol", "latency_ms": 15000.0, "failed": True},
            ]
        return []


def test_llm_response_metrics_aggregates_sources(monkeypatch):
    from app.services import llm_response_metrics

    conn = _Conn()
    monkeypatch.setattr(llm_response_metrics, "get_pool", lambda: _Pool(conn))

    result = asyncio.run(llm_response_metrics.get_llm_response_metrics(hours=7, model="gpt"))

    assert result["period_hours"] == 7
    assert result["model_filter"] == "gpt"
    assert result["summary"]["total_observations"] == 5
    assert result["summary"]["failed_observations"] == 1
    # 실패 판정이 없는 채팅 표본(3건)은 실패율 분모에서 빠진다.
    assert result["summary"]["failure_denominator"] == 2
    assert result["summary"]["failure_rate_pct"] == 50.0
    assert result["summary"]["invariant_violations"] == []
    assert result["metrics"]["chat_final_response"][0]["failed_calls"] is None
    assert result["metrics"]["chat_final_response"][0]["failure_rate_pct"] is None
    assert result["metrics"]["chat_final_response"][0]["p50_latency_ms"] == 1000
    assert result["metrics"]["runner_cli_total"][0]["model"] == "gpt-5.6-sol"
    assert result["summary"]["slowest_top5"][0]["source"] == "runner_cli_total"
    assert result["metrics"]["chat_final_response"][0]["provider"] == "openai"
    assert result["metrics"]["runner_cli_total"][0]["provider"] == "codex"
    assert result["runner_cli_phase_metrics"][0]["event_type"] == "cli_first_stdout"
    assert result["recent_runner_cli_events"][0]["event_type"] == "cli_process_started"
    assert {call[1].total_seconds() for call in conn.calls} == {25200.0}
    assert {call[2] for call in conn.calls} == {"gpt"}
