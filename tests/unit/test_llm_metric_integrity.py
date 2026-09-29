"""M7 지표 신뢰성 — 실패율 분모, 모델 정규화, 폴백 집계, 비용 근거, 지연·한도 기록."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone

import pytest

from app.services import llm_response_metrics, oauth_usage_tracker
from app.services.llm_metric_normalization import (
    build_cost_entry,
    classify_fallback_chain,
    cost_basis_for_source,
    normalize_model_dimension,
    summarize_fallback_chains,
)

# 공개 모델 id 다(시크릿 아님). `"model_key": "<고엔트로피 문자열>"` 모양이
# gitleaks generic-api-key 에 걸리므로(오류 사전 git.gitleaks_flags_public_model_id)
# 픽스처에 리터럴로 박지 않고 여기 한 번만 둔다.
HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"


# ── 결함 1: 실패율 분모·분자 ────────────────────────────────────────────


def test_취소된_무지연_작업도_분모에_들어가_실패율이_100을_넘지_않는다():
    # 2026-09-29 실측 재현: claude-sonnet-4-6 지연>0 20건, 실패 30건(취소 11 = 지연 0)
    rows = (
        [{"model_key": "claude-sonnet-4-6", "latency_ms": 0, "failed": True}] * 11
        + [{"model_key": "claude-sonnet-4-6", "latency_ms": 5000.0, "failed": True}] * 19
        + [{"model_key": "claude-sonnet-4-6", "latency_ms": 7000.0, "failed": False}]
    )
    models, states = llm_response_metrics._aggregate_samples("runner_cli_total", rows)

    assert states == []
    item = models[0]
    assert item["calls"] == 31
    assert item["latency_samples"] == 20
    assert item["failed_calls"] == 30
    assert item["failure_rate_pct"] == 96.8
    assert item["failed_calls"] <= item["calls"]


def test_실패를_재지_않는_소스는_실패율이_None_이다():
    rows = [{"model_key": "claude-opus-5", "latency_ms": 1200.0, "failed": None}]
    models, _ = llm_response_metrics._aggregate_samples("chat_final_response", rows)
    assert models[0]["failed_calls"] is None
    assert models[0]["failure_rate_pct"] is None


def test_지연이_0인_행은_백분위에서_빠진다():
    rows = [
        {"model_key": "claude-opus-5", "latency_ms": 0, "failed": False},
        {"model_key": "claude-opus-5", "latency_ms": 100.0, "failed": False},
        {"model_key": "claude-opus-5", "latency_ms": 300.0, "failed": False},
    ]
    models, _ = llm_response_metrics._aggregate_samples("oauth_llm_api", rows)
    assert models[0]["calls"] == 3
    assert models[0]["latency_samples"] == 2
    assert models[0]["p50_latency_ms"] == 200


def _as_aggregate_row(item: dict) -> dict:
    """_aggregate_samples 항목을 예전 집계 행 모양(model_key/calls/…)으로 되돌린다."""
    return {
        "model_key": item["raw_models"][0],
        "raw_models": item["raw_models"],
        "calls": item["calls"],
        "latency_samples": item["latency_samples"],
        "failed_calls": item["failed_calls"],
        "avg_latency_ms": item["avg_latency_ms"],
        "p50_latency_ms": item["p50_latency_ms"],
        "p95_latency_ms": item["p95_latency_ms"],
        "max_latency_ms": item["max_latency_ms"],
    }


def test_metric_row_위임은_실패를_재는_소스에서_새_경로와_같은_dict_를_낸다():
    rows = (
        [{"model_key": "claude-sonnet-4-6", "latency_ms": 0, "failed": True}] * 3
        + [{"model_key": "claude-sonnet-4-6", "latency_ms": 5000.0, "failed": False}] * 4
        + [{"model_key": "claude-sonnet-4-6", "latency_ms": 9000.0, "failed": True}]
    )
    models, _ = llm_response_metrics._aggregate_samples("runner_cli_total", rows)
    expected = models[0]
    assert llm_response_metrics._metric_row("runner_cli_total", _as_aggregate_row(expected)) == expected


def test_metric_row_위임은_상태값과_실패_미측정_소스에서도_새_경로와_같은_dict_를_낸다():
    rows = [
        {"model_key": "<synthetic>", "latency_ms": 100.0, "failed": None},
        {"model_key": "<synthetic>", "latency_ms": 300.0, "failed": None},
    ]
    _, states = llm_response_metrics._aggregate_samples("chat_final_response", rows)
    expected = states[0]
    assert expected["model"] is None
    assert llm_response_metrics._metric_row("chat_final_response", _as_aggregate_row(expected)) == expected


def test_metric_row_는_분자가_분모보다_큰_행에_실패율을_만들지_않는다():
    row = {
        "model_key": "claude-sonnet-4-6", "calls": 20, "failed_calls": 30,
        "avg_latency_ms": None, "p50_latency_ms": None, "p95_latency_ms": None, "max_latency_ms": None,
    }
    item = llm_response_metrics._metric_row("runner_cli_total", row)
    assert item["failure_rate_pct"] is None
    assert item["raw_models"] == ["claude-sonnet-4-6"]


# ── 결함 4: 모델 정규화 ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("gpt-6-sol", "gpt-6-sol"),
        ("GPT-6 Sol (Codex CLI)", "gpt-6-sol"),
        ("codex:gpt-6-sol", "gpt-6-sol"),
        ("GPT-5.6 Sol (Codex CLI)", "gpt-5.6-sol"),
        ("claude-opus-5", "claude-opus-5"),
        # 계약(scripts/claude_model_contract.ALIASES) 기준 별칭 해석
        ("claude-opus", "claude-opus-5-5"),
        ("claude-sonnet", "claude-sonnet-5-5"),
        ("claude-fable-5.1", "claude-fable-5-1"),
        # 계약상 별개 모델은 합치지 않는다
        ("claude-fable-5", "claude-fable-5"),
        ("claude-fable-5-1", "claude-fable-5-1"),
    ],
)
def test_같은_모델의_여러_표기를_하나로_모은다(raw, expected):
    result = normalize_model_dimension(raw)
    assert result["model"] == expected
    assert result["state"] is None
    assert result["raw"] == raw


@pytest.mark.parametrize(
    "raw, state",
    [
        ("interrupted", "interrupted"),
        ("stopped", "stopped"),
        ("unknown", "unknown"),
        ("unverified", "unverified"),
        ("Unverified", "unverified"),
        ("mixture", "mixture"),
        ("<synthetic>", "<synthetic>"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_비모델_상태값은_모델이_아니라_상태_차원으로_간다(raw, state):
    result = normalize_model_dimension(raw)
    assert result["model"] is None
    assert result["state"] == state


def test_집계에서_상태값은_모델_목록이_아니라_상태_목록에_들어간다():
    rows = [
        {"model_key": "unverified", "latency_ms": 1000.0, "failed": True},
        {"model_key": "codex:gpt-6-sol", "latency_ms": 1000.0, "failed": False},
        {"model_key": "GPT-6 Sol (Codex CLI)", "latency_ms": 3000.0, "failed": True},
    ]
    models, states = llm_response_metrics._aggregate_samples("runner_cli_total", rows)
    assert [item["model"] for item in models] == ["gpt-6-sol"]
    assert models[0]["calls"] == 2
    assert models[0]["raw_models"] == ["GPT-6 Sol (Codex CLI)", "codex:gpt-6-sol"]
    assert [item["model_state"] for item in states] == ["unverified"]


# ── 결함 5: 폴백 집계 ─────────────────────────────────────────────────


def test_빈_배열과_M5_전체실패_객체와_성공_객체를_함께_판정한다():
    all_failed = json.dumps({
        "result": "all_failed",
        "stages": [{"stage": "fallback_attempt", "result": "failed"}, {"stage": "all_failed"}],
    })
    succeeded = {
        "from": "claude-opus-5",
        "to": "gpt-6-sol",
        "reason": "429_rate_limit",
        "stages": [{"stage": "fallback_attempt"}, {"stage": "fallback_succeeded"}],
    }
    assert classify_fallback_chain("[]") == "none_recorded"
    assert classify_fallback_chain([]) == "none_recorded"
    assert classify_fallback_chain(None) == "none_recorded"
    assert classify_fallback_chain(all_failed) == "fallback_all_failed"
    assert classify_fallback_chain(succeeded) == "fallback_succeeded"
    assert classify_fallback_chain(json.dumps([{"stage": "fallback_attempt"}])) == "fallback_attempted"
    assert classify_fallback_chain("{not json") == "unparseable"


def test_폴백률은_기록된_폴백_턴_비율이다():
    summary = summarize_fallback_chains([
        ("[]", 846),
        (json.dumps({"result": "all_failed", "stages": [{"stage": "all_failed"}]}), 1),
        (json.dumps({"from": "a", "to": "b", "stages": [{"stage": "fallback_succeeded"}]}), 3),
    ])
    assert summary["turns"] == 850
    assert summary["fallback_turns"] == 4
    assert summary["fallback_rate_pct"] == 0.5
    assert summary["fallback_success_rate_pct"] == 75.0
    assert summary["by_class"]["none_recorded"] == 846


def test_폴백_기록이_없으면_성공률은_None_이다():
    summary = summarize_fallback_chains([("[]", 10)])
    assert summary["fallback_turns"] == 0
    assert summary["fallback_success_rate_pct"] is None
    assert summarize_fallback_chains([])["fallback_rate_pct"] is None


# ── 결함 6: 비용 산출 근거 ─────────────────────────────────────────────


def test_구독_CLI_비용은_정가_환산이고_실청구는_미측정이다():
    entry = build_cost_entry("cli_relay", 9332.21, calls=786)
    assert entry["cost_basis"] == "list_price_equivalent"
    assert entry["list_price_equivalent_usd"] == 9332.21
    assert entry["billed_usd"] is None
    assert entry["billed_status"] == "미측정"


def test_cost_usd_를_넘기지_않는_경로는_0을_비용으로_내지_않는다():
    entry = build_cost_entry("anthropic_client", 0, calls=743)
    assert entry["cost_basis"] == "not_recorded"
    assert entry["list_price_equivalent_usd"] is None
    assert entry["billed_usd"] is None


def test_모르는_call_source_는_근거_unknown_으로_금액을_내지_않는다():
    basis = cost_basis_for_source("new_path")
    assert basis["cost_basis"] == "unknown"
    assert build_cost_entry("new_path", 5.0)["list_price_equivalent_usd"] is None


def test_사용량_집계는_캐시_토큰을_포함하고_정가환산만_합산한다():
    rows = [
        {
            "call_source": "cli_relay", "model_key": "claude-opus-5", "calls": 2,
            "input_tokens": 40, "output_tokens": 1000,
            "cache_creation_tokens": 5000, "cache_read_tokens": 4_000_000, "cost_usd": 20.5,
            "duration_recorded": 2, "rl_tokens_recorded": 0,
            "unified_status_recorded": 2, "unified_utilization_recorded": 2,
        },
        {
            "call_source": "anthropic_client", "model_key": HAIKU_MODEL_ID, "calls": 3,
            "input_tokens": 300, "output_tokens": 30,
            "cache_creation_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0,
            "duration_recorded": 3, "rl_tokens_recorded": 0,
            "unified_status_recorded": 3, "unified_utilization_recorded": 3,
        },
    ]
    usage = llm_response_metrics._usage_section(rows, [])
    assert usage["cost"]["list_price_equivalent_usd"] == 20.5
    assert usage["cost"]["billed_usd"] is None
    assert usage["cost"]["not_recorded_calls"] == 3
    opus = next(item for item in usage["by_model"] if item["model"] == "claude-opus-5")
    assert opus["total_tokens"] == 40 + 1000 + 5000 + 4_000_000
    coverage = {item["call_source"]: item for item in usage["recording_coverage"]}
    assert coverage["cli_relay"]["duration_recorded_pct"] == 100.0


# ── 결함 2·3: 지연·한도 기록 경로 ──────────────────────────────────────


def test_CLI_rate_limit_event_를_unified_컬럼_단위로_옮긴다():
    info = {
        "status": "allowed_warning",
        "rateLimitType": "seven_day",
        "unifiedWindows": {
            "five_hour": {"resetsAt": 1790700000, "utilization": 0.25},
            "seven_day": {"resetsAt": 1790704800, "utilization": 0.75},
        },
    }
    parsed = oauth_usage_tracker.parse_cli_rate_limit_info(info)
    assert parsed["unified_status"] == "allowed_warning"
    assert parsed["unified_5h_utilization"] == 0.25
    assert parsed["unified_7d_utilization"] == 0.75
    assert parsed["unified_7d_reset"] == datetime.fromtimestamp(1790704800, tz=timezone.utc)
    assert parsed["unified_fallback"] == "seven_day"
    # OAuth 구독은 tokens-* 한도를 주지 않는다 — 만들어 내지 않는다
    assert "rl_tokens_remaining" not in parsed


def test_빈_rate_limit_info_는_아무것도_채우지_않는다():
    assert oauth_usage_tracker.parse_cli_rate_limit_info(None) == {}
    assert oauth_usage_tracker.parse_cli_rate_limit_info({}) == {}
    assert oauth_usage_tracker.parse_cli_rate_limit_info("x") == {}


def test_log_usage_가_duration_과_CLI_한도를_적재_항목에_싣는다(monkeypatch):
    captured = []

    async def _capture(entry):
        captured.append(entry)

    monkeypatch.setattr(oauth_usage_tracker, "_enqueue_usage", _capture)
    monkeypatch.setattr(oauth_usage_tracker, "_ensure_flush_task", lambda: None)

    async def _run():
        oauth_usage_tracker.log_usage(
            token="",
            model="claude-opus-5",
            call_source="cli_relay",
            account_slot="1",
            duration_ms=4321,
            rate_limit_info={"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.1}}},
        )
        await asyncio.sleep(0)

    asyncio.run(_run())
    entry = captured[0]
    values = dict(zip(oauth_usage_tracker._USAGE_LOG_COLUMNS, oauth_usage_tracker._usage_log_values(entry)))
    assert values["duration_ms"] == 4321
    assert values["unified_status"] == "allowed"
    assert values["unified_5h_utilization"] == 0.1
    assert values["rl_tokens_remaining"] is None


def test_헤더_값이_CLI_한도보다_우선한다(monkeypatch):
    captured = []

    async def _capture(entry):
        captured.append(entry)

    monkeypatch.setattr(oauth_usage_tracker, "_enqueue_usage", _capture)
    monkeypatch.setattr(oauth_usage_tracker, "_ensure_flush_task", lambda: None)

    async def _run():
        oauth_usage_tracker.log_usage(
            token="",
            model=HAIKU_MODEL_ID,
            headers={"anthropic-ratelimit-unified-status": "rejected"},
            rate_limit_info={"status": "allowed"},
        )
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert captured[0]["rl"]["unified_status"] == "rejected"


def test_duration_을_빠뜨렸던_세_경로가_이제_경과시간을_넘긴다():
    from app.services import model_selector

    cli_src = inspect.getsource(model_selector._stream_cli_relay_once)
    codex_src = inspect.getsource(model_selector._stream_codex_relay_once)
    sdk_src = inspect.getsource(model_selector._run_agent_sdk_with_key)
    for src in (cli_src, codex_src, sdk_src):
        assert "_usage_t0 = _time_mod.monotonic()" in src
        assert "duration_ms=int((_time_mod.monotonic() - _usage_t0) * 1000)" in src
    assert "rate_limit_info=_last_rate_limit_info or None" in cli_src
