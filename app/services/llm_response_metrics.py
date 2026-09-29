from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.db_pool import get_pool
from app.services.llm_account_usage import _classify_provider_from_model, _display_name_for_provider
from app.services.llm_metric_normalization import (
    build_cost_entry,
    normalize_model_dimension,
    summarize_fallback_chains,
)

KST = timezone(timedelta(hours=9))


def _round_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _round_pct(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return 0.0


def _percentile_cont(sorted_values: list[float], fraction: float) -> float | None:
    """PostgreSQL percentile_cont 와 같은 선형 보간."""
    if not sorted_values:
        return None
    position = fraction * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * weight


def _to_latency(value: Any) -> float | None:
    try:
        latency = float(value)
    except (TypeError, ValueError):
        return None
    return latency if latency > 0 else None


def _aggregate_samples(source: str, rows: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """표본 행을 (정규화 모델 | 비모델 상태) 로 묶는다.

    분모(calls)와 분자(failed_calls)는 **같은 행 집합**에서 센다. 예전에는 분모를
    "지연>0 인 행"만, 분자를 "전체 실패 행"으로 세서, 지연이 0 인 취소 작업이
    분자에만 들어가 실패율 150% 가 나왔다(2026-09-29 runner_cli_total
    claude-sonnet-4-6 calls=20 failed=30). 실패를 재지 않는 소스는 None 이다.
    """
    groups: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for row in rows:
        dimension = normalize_model_dimension(row["model_key"])
        key = (dimension["model"], dimension["state"])
        group = groups.setdefault(
            key,
            {"calls": 0, "failed": 0, "failure_measured": False, "latencies": [], "raw": Counter()},
        )
        group["calls"] += 1
        group["raw"][str(row["model_key"] or "")] += 1
        failed = row["failed"]
        if failed is not None:
            group["failure_measured"] = True
            if failed:
                group["failed"] += 1
        latency = _to_latency(row["latency_ms"])
        if latency is not None:
            group["latencies"].append(latency)

    models: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    for (model_name, state), group in groups.items():
        latencies = sorted(group["latencies"])
        calls = group["calls"]
        failed = group["failed"] if group["failure_measured"] else None
        raw_counter: Counter = group["raw"]
        top_raw = raw_counter.most_common(1)[0][0]
        provider = _classify_provider_from_model(top_raw)
        item = {
            "source": source,
            "model": model_name,
            "model_state": state,
            "raw_models": sorted(raw_counter),
            "provider": provider,
            "provider_label": _display_name_for_provider(provider),
            "calls": calls,
            "latency_samples": len(latencies),
            "failed_calls": failed,
            "failure_rate_pct": _round_pct(failed * 100.0 / calls) if failed is not None and calls else None,
            "avg_latency_ms": _round_ms(sum(latencies) / len(latencies)) if latencies else None,
            "p50_latency_ms": _round_ms(_percentile_cont(latencies, 0.50)),
            "p95_latency_ms": _round_ms(_percentile_cont(latencies, 0.95)),
            "max_latency_ms": _round_ms(latencies[-1]) if latencies else None,
        }
        (models if model_name is not None else states).append(item)

    order = lambda item: (-item["calls"], str(item["model"] or item["model_state"]))  # noqa: E731
    return sorted(models, key=order), sorted(states, key=order)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


# 보존 게이트가 모듈 private 헬퍼를 public 으로 오탐하므로 시그니처를 유지한다.
def _metric_row(source: str, row: Any) -> dict[str, Any]:
    """이미 집계된 행(model_key/calls/failed_calls/지연 통계)을 _aggregate_samples 항목 형태로 바꾼다.

    모델 정규화·실패율 규칙은 새 경로와 같다. failed_calls 가 None 이면 실패를
    재지 않는 소스로 보고, failed_calls > calls 인 행은 불변식 위반이므로
    실패율을 만들지 않는다(예전 150% 표기 재발 방지).
    """
    raw_model = str(row["model_key"] or "")
    dimension = normalize_model_dimension(raw_model)
    provider = _classify_provider_from_model(raw_model)
    calls = int(row["calls"] or 0)
    failed_value = row["failed_calls"]
    failed = int(failed_value) if failed_value is not None else None
    raw_models = _row_value(row, "raw_models")
    return {
        "source": source,
        "model": dimension["model"],
        "model_state": dimension["state"],
        "raw_models": sorted(raw_models) if raw_models is not None else [raw_model],
        "provider": provider,
        "provider_label": _display_name_for_provider(provider),
        "calls": calls,
        "latency_samples": int(_row_value(row, "latency_samples", calls) or 0),
        "failed_calls": failed,
        "failure_rate_pct": (
            _round_pct(failed * 100.0 / calls) if failed is not None and calls and failed <= calls else None
        ),
        "avg_latency_ms": _round_ms(row["avg_latency_ms"]),
        "p50_latency_ms": _round_ms(row["p50_latency_ms"]),
        "p95_latency_ms": _round_ms(row["p95_latency_ms"]),
        "max_latency_ms": _round_ms(row["max_latency_ms"]),
    }


async def _fetch_rows(conn: Any, sql: str, interval_value: timedelta, model: str) -> list[Any]:
    return list(await conn.fetch(sql, interval_value, model))


async def _fetch_optional_rows(conn: Any, sql: str, interval_value: timedelta, model: str) -> list[Any]:
    try:
        return list(await conn.fetch(sql, interval_value, model))
    except Exception:
        return []


def _phase_metric_row(row: Any) -> dict[str, Any]:
    model_key = str(row["model_key"] or "unknown")
    provider = _classify_provider_from_model(model_key)
    return {
        "event_type": str(row["event_type"] or ""),
        "model": model_key,
        "provider": provider,
        "provider_label": _display_name_for_provider(provider),
        "calls": int(row["calls"] or 0),
        "avg_duration_ms": _round_ms(row["avg_duration_ms"]),
        "p50_duration_ms": _round_ms(row["p50_duration_ms"]),
        "p95_duration_ms": _round_ms(row["p95_duration_ms"]),
        "max_duration_ms": _round_ms(row["max_duration_ms"]),
    }


def _event_row(row: Any) -> dict[str, Any]:
    observed_at = row["observed_at"]
    metadata = row["metadata"] or {}
    return {
        "job_id": row["job_id"],
        "project": row["project"],
        "event_type": row["event_type"],
        "status": row["status"],
        "phase": row["phase"],
        "model": row["model_key"],
        "size": row["size"],
        "duration_ms": row["duration_ms"],
        "observed_at": observed_at.isoformat() if observed_at else None,
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def _usage_section(usage_rows: list[Any], latest_limit_rows: list[Any]) -> dict[str, Any]:
    """oauth_usage_log 사용량·비용·한도. 비용은 산출 근거와 함께만 낸다."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    coverage: dict[str, dict[str, int]] = {}
    for row in usage_rows:
        call_source = str(row["call_source"] or "unknown")
        dimension = normalize_model_dimension(row["model_key"])
        model_name = dimension["model"] or f"state:{dimension['state']}"
        bucket = merged.setdefault(
            (call_source, model_name),
            {
                "call_source": call_source,
                "model": model_name,
                "raw_models": set(),
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        bucket["raw_models"].add(str(row["model_key"] or ""))
        for field in ("calls", "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens"):
            bucket[field] += int(row[field] or 0)
        bucket["cost_usd"] += float(row["cost_usd"] or 0)

        cov = coverage.setdefault(
            call_source,
            {
                "calls": 0,
                "duration_recorded": 0,
                "rl_tokens_recorded": 0,
                "unified_status_recorded": 0,
                "unified_utilization_recorded": 0,
            },
        )
        for field in cov:
            cov[field] += int(row[field] or 0)

    by_model: list[dict[str, Any]] = []
    list_price_total = 0.0
    not_recorded_calls = 0
    for bucket in merged.values():
        entry = build_cost_entry(bucket["call_source"], bucket["cost_usd"], calls=bucket["calls"])
        if entry["list_price_equivalent_usd"] is not None:
            list_price_total += entry["list_price_equivalent_usd"]
        else:
            not_recorded_calls += bucket["calls"]
        by_model.append({
            "model": bucket["model"],
            "raw_models": sorted(bucket["raw_models"]),
            "input_tokens": bucket["input_tokens"],
            "output_tokens": bucket["output_tokens"],
            "cache_creation_tokens": bucket["cache_creation_tokens"],
            "cache_read_tokens": bucket["cache_read_tokens"],
            "total_tokens": (
                bucket["input_tokens"] + bucket["output_tokens"]
                + bucket["cache_creation_tokens"] + bucket["cache_read_tokens"]
            ),
            **entry,
        })
    by_model.sort(key=lambda item: (-(item["list_price_equivalent_usd"] or 0), -item["calls"]))

    coverage_items = []
    for call_source, cov in sorted(coverage.items()):
        calls = cov["calls"]
        coverage_items.append({
            "call_source": call_source,
            **cov,
            "duration_recorded_pct": _round_pct(cov["duration_recorded"] * 100.0 / calls) if calls else None,
        })

    latest_limits = []
    for row in latest_limit_rows:
        latest_limits.append({
            "account_slot": row["account_slot"],
            "call_source": row["call_source"],
            "unified_status": row["unified_status"],
            "unified_5h_status": row["unified_5h_status"],
            "unified_5h_utilization": row["unified_5h_utilization"],
            "unified_5h_reset": row["unified_5h_reset"].isoformat() if row["unified_5h_reset"] else None,
            "unified_7d_status": row["unified_7d_status"],
            "unified_7d_utilization": row["unified_7d_utilization"],
            "unified_7d_reset": row["unified_7d_reset"].isoformat() if row["unified_7d_reset"] else None,
            "rl_tokens_limit": row["rl_tokens_limit"],
            "rl_tokens_remaining": row["rl_tokens_remaining"],
            "observed_at": row["created_at"].isoformat() if row["created_at"] else None,
        })

    return {
        "cost": {
            "list_price_equivalent_usd": round(list_price_total, 2),
            "billed_usd": None,
            "billed_status": "미측정",
            "not_recorded_calls": not_recorded_calls,
            "note": (
                "OAuth/Codex 구독 호출의 정가 환산값이다. 실청구액이 아니며 실청구는 측정하지 않는다. "
                "cost_basis=not_recorded 인 경로는 cost_usd 를 적지 않으므로 합계에서 뺐다."
            ),
        },
        "by_model": by_model,
        "recording_coverage": coverage_items,
        "latest_limits": latest_limits,
        "limit_note": (
            "OAuth 구독 응답은 anthropic-ratelimit-tokens-* 헤더를 주지 않고 "
            "anthropic-ratelimit-unified-* (5시간/7일 사용률)만 준다. rl_tokens_* 가 NULL 인 것은 "
            "정상이며 한도 지표는 unified_* 컬럼이다."
        ),
    }


def _chat_turn_section(model_rows: list[Any], fallback_rows: list[Any]) -> dict[str, Any]:
    """chat_turn_executions — 모델 차원과 상태 차원을 나누고 폴백률을 센다."""
    models: dict[str, dict[str, Any]] = {}
    states: Counter = Counter()
    statuses: Counter = Counter()
    for row in model_rows:
        turns = int(row["turns"] or 0)
        status = str(row["status"] or "unknown")
        statuses[status] += turns
        dimension = normalize_model_dimension(row["model_key"])
        if dimension["model"] is None:
            states[dimension["state"]] += turns
            continue
        bucket = models.setdefault(
            dimension["model"], {"model": dimension["model"], "raw_models": set(), "turns": 0, "by_status": Counter()}
        )
        bucket["raw_models"].add(str(row["model_key"] or ""))
        bucket["turns"] += turns
        bucket["by_status"][status] += turns

    model_items = [
        {
            "model": bucket["model"],
            "raw_models": sorted(bucket["raw_models"]),
            "turns": bucket["turns"],
            "by_status": dict(bucket["by_status"]),
        }
        for bucket in models.values()
    ]
    model_items.sort(key=lambda item: (-item["turns"], item["model"]))
    return {
        "by_model": model_items,
        "unattributed_by_state": dict(states),
        "by_status": dict(statuses),
        "fallback": summarize_fallback_chains((row["fallback_chain"], row["turns"]) for row in fallback_rows),
    }


async def get_llm_response_metrics(*, hours: int = 24, model: str = "") -> dict[str, Any]:
    """Aggregate measured LLM latency from chat, API usage, and CLI runner tables."""
    hours = max(1, min(int(hours or 24), 168))
    interval_value = timedelta(hours=hours)
    model_filter = (model or "").strip()

    # 표본 단위로 읽는다 — 모델 별칭 정규화 뒤에 다시 묶어야 하므로 SQL 에서
    # 원시 모델명 기준으로 집계하면 백분위를 합칠 수 없다. failed 는 실패를
    # 재지 않는 소스에서 NULL 이다(채팅 최종 응답에는 실패 판정이 없다).
    chat_sql = """
        SELECT
            COALESCE(NULLIF(model_used, ''), 'unknown') AS model_key,
            COALESCE(
                CASE WHEN (quality_details->>'response_duration_ms') ~ '^[0-9]+(\\.[0-9]+)?$'
                     THEN (quality_details->>'response_duration_ms')::numeric END,
                CASE WHEN (quality_details->>'duration_ms') ~ '^[0-9]+(\\.[0-9]+)?$'
                     THEN (quality_details->>'duration_ms')::numeric END
            ) AS latency_ms,
            NULL::boolean AS failed
        FROM chat_messages
        WHERE role = 'assistant'
          AND created_at >= NOW() - ($1::interval)
          AND ($2::text = '' OR COALESCE(model_used, '') ILIKE ('%' || $2::text || '%'))
    """

    oauth_sql = """
        SELECT
            COALESCE(NULLIF(model, ''), 'unknown') AS model_key,
            duration_ms AS latency_ms,
            (error_code IS NOT NULL) AS failed
        FROM oauth_usage_log
        WHERE created_at >= NOW() - ($1::interval)
          AND ($2::text = '' OR COALESCE(model, '') ILIKE ('%' || $2::text || '%'))
    """

    bg_sql = """
        SELECT
            COALESCE(NULLIF(model, ''), 'unknown') AS model_key,
            latency_ms,
            (success = FALSE) AS failed
        FROM bg_llm_usage_log
        WHERE created_at >= NOW() - ($1::interval)
          AND ($2::text = '' OR COALESCE(model, '') ILIKE ('%' || $2::text || '%'))
    """

    runner_sql = """
        SELECT
            COALESCE(NULLIF(actual_model, ''), NULLIF(worker_model, ''), NULLIF(model, ''), 'unknown') AS model_key,
            EXTRACT(EPOCH FROM (COALESCE(completed_at, updated_at) - COALESCE(started_at, created_at))) * 1000 AS latency_ms,
            status NOT IN ('done', 'awaiting_approval') AS failed
        FROM pipeline_jobs
        WHERE COALESCE(started_at, created_at) >= NOW() - ($1::interval)
          AND status IN ('done', 'awaiting_approval', 'error', 'cancelled', 'rejected_done', 'review_hold')
          AND ($2::text = '' OR COALESCE(actual_model, worker_model, model, '') ILIKE ('%' || $2::text || '%'))
    """

    # 사용량·비용·한도 적재 상태 (oauth_usage_log). 캐시 토큰을 반드시 포함한다 —
    # 채팅 CLI 호출은 입력 토큰이 수십 개이고 나머지 수백만이 cache_read 다.
    usage_sql = """
        SELECT
            COALESCE(NULLIF(call_source, ''), 'unknown') AS call_source,
            COALESCE(NULLIF(model, ''), 'unknown') AS model_key,
            COUNT(*) AS calls,
            COALESCE(SUM(input_tokens), 0) AS input_tokens,
            COALESCE(SUM(output_tokens), 0) AS output_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cost_usd), 0) AS cost_usd,
            COUNT(*) FILTER (WHERE duration_ms IS NOT NULL AND duration_ms > 0) AS duration_recorded,
            COUNT(*) FILTER (WHERE rl_tokens_remaining IS NOT NULL) AS rl_tokens_recorded,
            COUNT(*) FILTER (WHERE unified_status IS NOT NULL) AS unified_status_recorded,
            COUNT(*) FILTER (WHERE unified_5h_utilization IS NOT NULL
                               OR unified_7d_utilization IS NOT NULL) AS unified_utilization_recorded
        FROM oauth_usage_log
        WHERE created_at >= NOW() - ($1::interval)
          AND ($2::text = '' OR COALESCE(model, '') ILIKE ('%' || $2::text || '%'))
        GROUP BY 1, 2
    """

    latest_limit_sql = """
        SELECT DISTINCT ON (account_slot)
            account_slot,
            call_source,
            unified_status,
            unified_5h_status,
            unified_5h_utilization,
            unified_5h_reset,
            unified_7d_status,
            unified_7d_utilization,
            unified_7d_reset,
            rl_tokens_limit,
            rl_tokens_remaining,
            created_at
        FROM oauth_usage_log
        WHERE created_at >= NOW() - ($1::interval)
          AND ($2::text = '' OR COALESCE(model, '') ILIKE ('%' || $2::text || '%'))
          AND (unified_status IS NOT NULL OR rl_tokens_remaining IS NOT NULL)
        ORDER BY account_slot, created_at DESC
    """

    chat_turn_model_sql = """
        SELECT
            COALESCE(NULLIF(actual_model, ''), 'unknown') AS model_key,
            COALESCE(NULLIF(status, ''), 'unknown') AS status,
            COUNT(*) AS turns
        FROM chat_turn_executions
        WHERE created_at >= NOW() - ($1::interval)
          AND ($2::text = '' OR COALESCE(actual_model, requested_model, '') ILIKE ('%' || $2::text || '%'))
        GROUP BY 1, 2
    """

    # 폴백 기록은 모양이 여럿이다(빈 배열 / 성공 객체 / 전체 실패 객체).
    # 원문 그대로 묶어 오면 빈 배열은 한 줄로 모이고, 판정은 한 함수가 한다.
    chat_turn_fallback_sql = """
        SELECT
            fallback_chain::text AS fallback_chain,
            COUNT(*) AS turns
        FROM chat_turn_executions
        WHERE created_at >= NOW() - ($1::interval)
          AND ($2::text = '' OR COALESCE(actual_model, requested_model, '') ILIKE ('%' || $2::text || '%'))
        GROUP BY 1
    """

    runner_phase_sql = """
        SELECT
            event_type,
            COALESCE(NULLIF(actual_model, ''), NULLIF(model, ''), 'unknown') AS model_key,
            COUNT(*) AS calls,
            AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL AND duration_ms >= 0) AS avg_duration_ms,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ms)
                FILTER (WHERE duration_ms IS NOT NULL AND duration_ms >= 0) AS p50_duration_ms,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                FILTER (WHERE duration_ms IS NOT NULL AND duration_ms >= 0) AS p95_duration_ms,
            MAX(duration_ms) FILTER (WHERE duration_ms IS NOT NULL AND duration_ms >= 0) AS max_duration_ms
        FROM pipeline_runner_events
        WHERE observed_at >= NOW() - ($1::interval)
          AND event_type IN (
              'model_attempt_started',
              'cli_process_started',
              'cli_first_stdout',
              'cli_first_stderr',
              'model_attempt_completed',
              'actual_model_selected',
              'model_attempt_skipped'
          )
          AND ($2::text = '' OR COALESCE(actual_model, model, '') ILIKE ('%' || $2::text || '%'))
        GROUP BY event_type, model_key
        ORDER BY calls DESC, event_type ASC, model_key ASC
    """

    recent_runner_events_sql = """
        SELECT
            job_id,
            project,
            event_type,
            status,
            phase,
            COALESCE(NULLIF(actual_model, ''), NULLIF(model, ''), 'unknown') AS model_key,
            COALESCE(NULLIF(size, ''), 'M') AS size,
            duration_ms,
            observed_at,
            metadata
        FROM pipeline_runner_events
        WHERE observed_at >= NOW() - ($1::interval)
          AND event_type IN (
              'model_attempt_started',
              'cli_process_started',
              'cli_first_stdout',
              'cli_first_stderr',
              'model_attempt_completed',
              'actual_model_selected',
              'model_attempt_skipped'
          )
          AND ($2::text = '' OR COALESCE(actual_model, model, '') ILIKE ('%' || $2::text || '%'))
        ORDER BY observed_at DESC
        LIMIT 100
    """

    pool = get_pool()
    async with pool.acquire() as conn:
        source_rows = {
            "chat_final_response": await _fetch_rows(conn, chat_sql, interval_value, model_filter),
            "oauth_llm_api": await _fetch_rows(conn, oauth_sql, interval_value, model_filter),
            "background_llm": await _fetch_rows(conn, bg_sql, interval_value, model_filter),
            "runner_cli_total": await _fetch_rows(conn, runner_sql, interval_value, model_filter),
        }
        runner_phase_rows = await _fetch_optional_rows(conn, runner_phase_sql, interval_value, model_filter)
        recent_runner_event_rows = await _fetch_optional_rows(conn, recent_runner_events_sql, interval_value, model_filter)
        usage_rows = await _fetch_optional_rows(conn, usage_sql, interval_value, model_filter)
        latest_limit_rows = await _fetch_optional_rows(conn, latest_limit_sql, interval_value, model_filter)
        chat_turn_model_rows = await _fetch_optional_rows(conn, chat_turn_model_sql, interval_value, model_filter)
        chat_turn_fallback_rows = await _fetch_optional_rows(conn, chat_turn_fallback_sql, interval_value, model_filter)

    metrics: list[dict[str, Any]] = []
    by_source: dict[str, list[dict[str, Any]]] = {}
    unattributed: dict[str, list[dict[str, Any]]] = {}
    for source, rows in source_rows.items():
        model_items, state_items = _aggregate_samples(source, rows)
        by_source[source] = model_items
        unattributed[source] = state_items
        metrics.extend(model_items)
        metrics.extend(state_items)

    total_calls = sum(item["calls"] for item in metrics)
    failure_items = [item for item in metrics if item["failed_calls"] is not None]
    failure_denominator = sum(item["calls"] for item in failure_items)
    total_failed = sum(item["failed_calls"] for item in failure_items)
    slowest_top5 = sorted(
        [item for item in metrics if item["model"] is not None and item["p95_latency_ms"] is not None],
        key=lambda item: int(item["p95_latency_ms"] or 0),
        reverse=True,
    )[:5]

    return {
        "period_hours": hours,
        "model_filter": model_filter or None,
        "generated_at_kst": datetime.now(KST).isoformat(),
        "summary": {
            "total_observations": total_calls,
            "failed_observations": total_failed,
            "failure_denominator": failure_denominator,
            "failure_rate_pct": _round_pct((total_failed * 100.0 / failure_denominator) if failure_denominator else 0),
            "failure_rate_basis": (
                "실패를 재는 소스(oauth_llm_api·background_llm·runner_cli_total)의 같은 행 집합. "
                "chat_final_response 는 실패 판정이 없어 분모에서 뺀다. 소스끼리 겹칠 수 있다 "
                "(채팅 CLI 호출은 chat_final_response 와 oauth_llm_api 양쪽에 남는다)."
            ),
            "invariant_violations": [
                {"source": item["source"], "model": item["model"], "model_state": item["model_state"]}
                for item in failure_items
                if item["failed_calls"] > item["calls"]
            ],
            "slowest_top5": slowest_top5,
        },
        "sources": {
            "chat_final_response": "chat_messages.quality_details.response_duration_ms|duration_ms",
            "oauth_llm_api": "oauth_usage_log.duration_ms (호출 전체 경과, 최초 토큰 지연 아님)",
            "background_llm": "bg_llm_usage_log.latency_ms",
            "runner_cli_total": "pipeline_jobs started_at/created_at -> completed_at/updated_at elapsed",
            "runner_cli_phase_metrics": "pipeline_runner_events model_attempt/cli_process/first_output/completed events",
            "chat_turns": "chat_turn_executions.actual_model/status/fallback_chain",
        },
        "runner_cli_phase_metrics": [_phase_metric_row(row) for row in runner_phase_rows],
        "recent_runner_cli_events": [_event_row(row) for row in recent_runner_event_rows],
        "metrics": by_source,
        "unattributed_by_state": unattributed,
        "usage": _usage_section(usage_rows, latest_limit_rows),
        "chat_turns": _chat_turn_section(chat_turn_model_rows, chat_turn_fallback_rows),
    }
