"""AADS-LLM-M7-MODEL-QUALITY-VISIBILITY 회귀시험.

- 모델 귀속: unverified / NULL / 시도 없음 세 갈래
- 성공률: 실패 3분류 분리, 표본 부족 규약
- 폴백: 첫 시도 모델 기준 out, 마지막 시도 모델 기준 in
- 지연: 표본 부족이면 NULL, 원천 없음이면 '미측정'
- 비용: llm_cost_basis 항목을 다시 계산하지 않고 cost_source 별로 붙인다
- 분류 뷰를 새로 만들지 않고 pipeline_failure_taxonomy 상수를 재사용한다
"""
from __future__ import annotations

import asyncio

from app.services import llm_model_quality as q
from app.services import pipeline_failure_taxonomy as tax


def _job(job_id, status, *, actual=None, last=None, first=None, attempts=None, cls=None, sub=None):
    return {
        "job_id": job_id,
        "status": status,
        "actual_model": actual,
        "requested_model": "codex:gpt-6-sol",
        "attempts": attempts if attempts is not None else (1 if last else 0),
        "first_model": first or last,
        "last_model": last,
        "failure_class": cls,
        "failure_subtype": sub,
    }


# ── 모델 귀속 ─────────────────────────────────────────────────────────

def test_unverified_actual_model_is_recovered_from_last_attempt():
    row = _job("a", "done", actual="unverified", last="claude-sonnet-5-5")
    attr = q.attribute_model(row)
    assert attr["model"] == "claude-sonnet-5-5"
    assert attr["source"] == q.ATTRIBUTION_LAST_ATTEMPT


def test_real_actual_model_wins_and_codex_prefix_is_normalized():
    attr = q.attribute_model(_job("a", "done", actual="codex:gpt-6-sol", last="codex:gpt-6-sol"))
    assert attr["model"] == "gpt-6-sol"
    assert attr["source"] == q.ATTRIBUTION_ACTUAL_MODEL


def test_no_attempt_is_not_charged_to_requested_model():
    attr = q.attribute_model(_job("a", "error", actual=None, last=None))
    assert attr["model"] is None
    assert attr["source"] == q.ATTRIBUTION_NO_ATTEMPT


def test_unverified_without_events_stays_unattributed():
    attr = q.attribute_model(_job("a", "error", actual="unverified", last=None))
    assert attr["source"] == q.ATTRIBUTION_NO_ATTEMPT


# ── 성공률: 실패 분류 분리 ─────────────────────────────────────────────

def _rows_for_model(done, defect, infra, gate, model="claude-sonnet-5-5"):
    rows = [_job(f"d{i}", "done", actual="unverified", last=model) for i in range(done)]
    rows += [_job(f"x{i}", "error", last=model, cls=tax.CLASS_INSTRUCTION, sub="review_request_changes") for i in range(defect)]
    rows += [_job(f"i{i}", "error", last=model, cls=tax.CLASS_INFRA, sub="rate_limit") for i in range(infra)]
    rows += [_job(f"g{i}", "cancelled", last=model, cls=tax.CLASS_GATE, sub="dedup_gate") for i in range(gate)]
    return rows


def test_attributable_rate_excludes_infra_and_gate():
    (item,) = q.aggregate_jobs(_rows_for_model(done=6, defect=2, infra=10, gate=5), "model")
    assert item["terminal_jobs"] == 23
    assert item["success_rate"]["value_pct"] == round(6 * 100 / 23, 1)
    attributable = item["model_attributable_success_rate"]
    assert attributable["n"] == 8
    assert attributable["value_pct"] == 75.0
    assert item["failed_by_class"] == {
        tax.CLASS_INSTRUCTION: 2, tax.CLASS_INFRA: 10, tax.CLASS_GATE: 5, tax.CLASS_UNCLASSIFIED: 0,
    }


def test_failure_classes_come_from_taxonomy_not_a_new_scheme():
    (item,) = q.aggregate_jobs(_rows_for_model(done=5, defect=1, infra=1, gate=1), "model")
    assert set(item["failed_by_class"]) == set(tax.FAILURE_CLASSES)
    assert q.VIEW_NAME == tax.VIEW_NAME


def test_missing_or_unknown_failure_class_counts_as_unclassified():
    rows = [
        _job("a", "error", last="claude-sonnet-5-5", cls=None),
        _job("b", "error", last="claude-sonnet-5-5", cls="something_new"),
    ]
    (item,) = q.aggregate_jobs(rows, "model")
    assert item["failed_by_class"][tax.CLASS_UNCLASSIFIED] == 2


def test_small_sample_reports_insufficient_sample_without_value():
    (item,) = q.aggregate_jobs(_rows_for_model(done=2, defect=1, infra=0, gate=0), "model")
    assert item["success_rate"] == {"value_pct": None, "n": 3, "status": "insufficient_sample"}


def test_rejected_done_is_not_success_and_not_a_classified_failure():
    rows = _rows_for_model(done=5, defect=0, infra=0, gate=0)
    rows.append(_job("r", "rejected_done", last="claude-sonnet-5-5"))
    (item,) = q.aggregate_jobs(rows, "model")
    assert item["terminal_jobs"] == 6
    assert item["failed_jobs"] == 0
    assert item["success_rate"]["value_pct"] == round(5 * 100 / 6, 1)


def test_no_attempt_bucket_is_marked_not_comparable():
    rows = [_job(f"g{i}", "cancelled", cls=tax.CLASS_GATE, sub="dependency_block") for i in range(6)]
    (item,) = q.aggregate_jobs(rows, "model")
    assert item["key"] == q.NO_ATTEMPT_LABEL
    assert item["comparable"] is False
    assert item["model_attribution"][q.ATTRIBUTION_NO_ATTEMPT] == 6


# ── 폴백 ─────────────────────────────────────────────────────────────

def test_fallback_out_counts_first_attempt_model_and_in_counts_last():
    rows = [
        _job("a", "done", actual="unverified", first="codex:gpt-6-sol", last="claude-sonnet-5-5", attempts=2),
        _job("b", "error", actual="codex:gpt-6-sol", first="codex:gpt-6-sol", last="codex:gpt-6-sol", attempts=1,
             cls=tax.CLASS_INFRA, sub="rate_limit"),
        _job("c", "done", actual="unverified", first="claude-sonnet-5-5", last="claude-sonnet-5-5", attempts=2),
    ]
    items = {i["key"]: i for i in q.aggregate_jobs(rows, "model")}
    codex = items["gpt-6-sol"]["fallback"]
    assert codex["first_attempt_jobs"] == 2
    assert codex["fallback_out_jobs"] == 1
    claude = items["claude-sonnet-5-5"]["fallback"]
    assert claude["fallback_in_jobs"] == 1
    assert claude["same_model_retry_jobs"] == 1
    assert claude["fallback_out_jobs"] == 0


def test_provider_and_route_dimensions():
    rows = [
        _job("a", "done", actual="codex:gpt-6-sol", last="codex:gpt-6-sol"),
        _job("b", "done", actual="unverified", last="claude-opus-5-5"),
    ]
    provider = {i["key"] for i in q.aggregate_jobs(rows, "provider")}
    route = {i["key"] for i in q.aggregate_jobs(rows, "route")}
    assert provider == {"codex", "anthropic"}
    assert route == {q.ROUTE_CODEX_CLI, q.ROUTE_CLAUDE_CLI}


# ── 지연 ─────────────────────────────────────────────────────────────

def test_latency_summary_states():
    assert q.latency_summary([])["status"] == "미측정"
    small = q.latency_summary([100, 200])
    assert small["status"] == "insufficient_sample" and small["p50_ms"] is None and small["n"] == 2
    ok = q.latency_summary([100, 200, 300, 400, 500])
    assert ok["status"] == "ok" and ok["p50_ms"] == 300 and ok["p95_ms"] == 480


def test_attach_latency_separates_stdout_and_stderr_and_notes_not_first_token():
    items = q.aggregate_jobs(_rows_for_model(done=5, defect=0, infra=0, gate=0), "model")
    events = [
        {"event_type": "cli_first_stdout", "model": "claude-sonnet-5-5", "duration_ms": 1000 * i}
        for i in range(1, 6)
    ] + [{"event_type": "cli_first_stderr", "model": "claude-sonnet-5-5", "duration_ms": 50}]
    q.attach_latency(items, "model", events)
    lat = items[0]["first_output_latency"]
    assert lat["first_stdout"]["status"] == "ok"
    assert lat["first_stderr"]["status"] == "insufficient_sample"
    assert "첫 토큰 지연은 미측정" in lat["basis"]


def test_chat_first_token_groups_by_normalized_model_and_moves_states_out():
    rows = [{"model": "codex:gpt-6-astra", "first_token_ms": 100 * i, "total_ms": 1000} for i in range(1, 7)]
    rows += [{"model": "gpt-6-astra", "first_token_ms": 700, "total_ms": 1000}]
    rows += [{"model": "mixture", "first_token_ms": 10, "total_ms": 20}, {"model": None, "first_token_ms": None, "total_ms": None}]
    items = {i["key"]: i for i in q.summarize_chat_first_token(rows)}
    assert items["gpt-6-astra"]["turns"] == 7
    assert items["gpt-6-astra"]["first_token"]["status"] == "ok"
    assert items["(mixture)"]["comparable"] is False
    assert items["(unknown)"]["first_token"]["status"] == "미측정"


# ── 비용 ─────────────────────────────────────────────────────────────

def test_attach_cost_keeps_cost_sources_apart_and_only_runner_surface():
    items = q.aggregate_jobs(_rows_for_model(done=5, defect=0, infra=0, gate=0, model="claude-opus-5-5"), "model")
    cost_items = [
        {"surface": "runner", "model": "claude-opus-5-5", "call_source": "runner_claude_cli",
         "cost_source": "relay_reported", "samples": 3, "total_cost_usd": 1.5, "catalog_cost_usd": 1.0,
         "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_creation_tokens": 0},
        {"surface": "runner", "model": "claude-opus-5-5", "call_source": "runner_claude_cli",
         "cost_source": "unknown", "samples": 2, "total_cost_usd": None, "catalog_cost_usd": None,
         "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_creation_tokens": 0},
        {"surface": "chat", "model": "claude-opus-5-5", "call_source": "cli_relay",
         "cost_source": "relay_reported", "samples": 99, "total_cost_usd": 99.0, "catalog_cost_usd": None,
         "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0},
    ]
    q.attach_cost(items, "model", cost_items)
    cost = items[0]["cost"]
    by_src = {c["cost_source"]: c for c in cost["by_cost_source"]}
    assert by_src["relay_reported"]["total_cost_usd"] == 1.5
    assert by_src["unknown"]["total_cost_usd"] is None
    assert cost["billed_usd"] is None
    assert "실청구 미측정" in cost["basis"]


def test_attach_cost_without_source_rows_is_unmeasured_not_zero():
    items = q.aggregate_jobs(_rows_for_model(done=5, defect=0, infra=0, gate=0), "model")
    q.attach_cost(items, "model", [])
    assert items[0]["cost"]["by_cost_source"] is None
    assert items[0]["cost"]["status"] == "미측정"


# ── 창 요약 ───────────────────────────────────────────────────────────

def test_summarize_window_counts_unverified_recovery():
    rows = [
        _job("a", "done", actual="unverified", last="claude-sonnet-5-5"),
        _job("b", "error", actual="unverified", last=None, cls=tax.CLASS_INFRA, sub="watchdog_stall"),
        _job("c", "error", actual=None, last=None, cls=tax.CLASS_GATE, sub="dependency_block"),
    ]
    s = q.summarize_window(rows)
    assert s["unverified_actual_model"] == {"recorded_jobs": 2, "recovered_by_runner_events": 1, "still_unattributed": 1}
    assert s["failed_by_class"][tax.CLASS_INFRA] == 1
    assert s["model_attribution"][q.ATTRIBUTION_NO_ATTEMPT] == 2


def test_fetch_model_quality_shape_with_fake_connection(monkeypatch):
    calls = []

    class FakeConn:
        async def fetch(self, sql, *args):
            calls.append(sql)
            if "pipeline_runner_events e" in sql and "model_attempt_started" in sql:
                return [_job("a", "done", actual="unverified", last="claude-sonnet-5-5")]
            return []

    async def fake_cost(conn, days):
        return {"items": []}

    monkeypatch.setattr(q, "fetch_cost_per_success", fake_cost)
    payload = asyncio.run(q.fetch_model_quality(FakeConn(), 7))
    assert {"by_model", "by_provider", "by_route", "chat_first_token", "summary", "sources", "notes"} <= set(payload)
    assert payload["by_model"][0]["key"] == "claude-sonnet-5-5"
    assert any(tax.VIEW_NAME in sql for sql in calls)


def test_attach_cost_maps_codex_slug_to_provider_and_route_dimensions():
    rows = [_job("a", "done", actual="codex:gpt-5.6-sol", last="codex:gpt-5.6-sol")]
    cost_items = [{"surface": "runner", "model": "gpt-5.6-sol", "cost_source": "relay_reported", "samples": 2,
                   "total_cost_usd": 0.5, "catalog_cost_usd": None, "input_tokens": 1, "output_tokens": 1,
                   "cache_read_tokens": 0, "cache_creation_tokens": 0}]
    for dim, key in (("model", "gpt-5.6-sol"), ("provider", "codex"), ("route", q.ROUTE_CODEX_CLI)):
        items = q.aggregate_jobs(rows, dim)
        q.attach_cost(items, dim, cost_items)
        assert items[0]["key"] == key
        assert items[0]["cost"]["status"] == "ok", dim
