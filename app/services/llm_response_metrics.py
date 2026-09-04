from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.db_pool import get_pool
from app.services.llm_account_usage import _classify_provider_from_model, _display_name_for_provider

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


def _metric_row(source: str, row: Any) -> dict[str, Any]:
    model_key = str(row["model_key"] or "unknown")
    provider = _classify_provider_from_model(model_key)
    calls = int(row["calls"] or 0)
    failed = int(row["failed_calls"] or 0)
    return {
        "source": source,
        "model": model_key,
        "provider": provider,
        "provider_label": _display_name_for_provider(provider),
        "calls": calls,
        "failed_calls": failed,
        "failure_rate_pct": _round_pct((failed * 100.0 / calls) if calls else 0),
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


async def get_llm_response_metrics(*, hours: int = 24, model: str = "") -> dict[str, Any]:
    """Aggregate measured LLM latency from chat, API usage, and CLI runner tables."""
    hours = max(1, min(int(hours or 24), 168))
    interval_value = timedelta(hours=hours)
    model_filter = (model or "").strip()

    chat_sql = """
        WITH samples AS (
            SELECT
                COALESCE(NULLIF(model_used, ''), 'unknown') AS model_key,
                COALESCE(
                    CASE WHEN (quality_details->>'response_duration_ms') ~ '^[0-9]+(\\.[0-9]+)?$'
                         THEN (quality_details->>'response_duration_ms')::numeric END,
                    CASE WHEN (quality_details->>'duration_ms') ~ '^[0-9]+(\\.[0-9]+)?$'
                         THEN (quality_details->>'duration_ms')::numeric END
                ) AS latency_ms,
                FALSE AS failed
            FROM chat_messages
            WHERE role = 'assistant'
              AND created_at >= NOW() - ($1::interval)
              AND ($2::text = '' OR COALESCE(model_used, '') ILIKE ('%' || $2::text || '%'))
        )
        SELECT
            model_key,
            COUNT(*) FILTER (WHERE latency_ms IS NOT NULL) AS calls,
            COUNT(*) FILTER (WHERE failed) AS failed_calls,
            AVG(latency_ms) AS avg_latency_ms,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50_latency_ms,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms,
            MAX(latency_ms) AS max_latency_ms
        FROM samples
        WHERE latency_ms IS NOT NULL
        GROUP BY model_key
        ORDER BY calls DESC, model_key ASC
    """

    oauth_sql = """
        SELECT
            COALESCE(NULLIF(model, ''), 'unknown') AS model_key,
            COUNT(*) FILTER (WHERE duration_ms IS NOT NULL AND duration_ms > 0) AS calls,
            COUNT(*) FILTER (WHERE error_code IS NOT NULL) AS failed_calls,
            AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL AND duration_ms > 0) AS avg_latency_ms,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ms)
                FILTER (WHERE duration_ms IS NOT NULL AND duration_ms > 0) AS p50_latency_ms,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                FILTER (WHERE duration_ms IS NOT NULL AND duration_ms > 0) AS p95_latency_ms,
            MAX(duration_ms) FILTER (WHERE duration_ms IS NOT NULL AND duration_ms > 0) AS max_latency_ms
        FROM oauth_usage_log
        WHERE created_at >= NOW() - ($1::interval)
          AND ($2::text = '' OR COALESCE(model, '') ILIKE ('%' || $2::text || '%'))
        GROUP BY model_key
        HAVING COUNT(*) FILTER (WHERE duration_ms IS NOT NULL AND duration_ms > 0) > 0
        ORDER BY calls DESC, model_key ASC
    """

    bg_sql = """
        SELECT
            COALESCE(NULLIF(model, ''), 'unknown') AS model_key,
            COUNT(*) FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) AS calls,
            COUNT(*) FILTER (WHERE success = FALSE) AS failed_calls,
            AVG(latency_ms) FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) AS avg_latency_ms,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms)
                FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) AS p50_latency_ms,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) AS p95_latency_ms,
            MAX(latency_ms) FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) AS max_latency_ms
        FROM bg_llm_usage_log
        WHERE created_at >= NOW() - ($1::interval)
          AND ($2::text = '' OR COALESCE(model, '') ILIKE ('%' || $2::text || '%'))
        GROUP BY model_key
        HAVING COUNT(*) FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) > 0
        ORDER BY calls DESC, model_key ASC
    """

    runner_sql = """
        WITH samples AS (
            SELECT
                COALESCE(NULLIF(actual_model, ''), NULLIF(worker_model, ''), NULLIF(model, ''), 'unknown') AS model_key,
                EXTRACT(EPOCH FROM (COALESCE(completed_at, updated_at) - COALESCE(started_at, created_at))) * 1000 AS latency_ms,
                status NOT IN ('done', 'awaiting_approval') AS failed
            FROM pipeline_jobs
            WHERE COALESCE(started_at, created_at) >= NOW() - ($1::interval)
              AND status IN ('done', 'awaiting_approval', 'error', 'cancelled', 'rejected_done', 'review_hold')
              AND ($2::text = '' OR COALESCE(actual_model, worker_model, model, '') ILIKE ('%' || $2::text || '%'))
        )
        SELECT
            model_key,
            COUNT(*) FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) AS calls,
            COUNT(*) FILTER (WHERE failed) AS failed_calls,
            AVG(latency_ms) FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) AS avg_latency_ms,
            percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms)
                FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) AS p50_latency_ms,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) AS p95_latency_ms,
            MAX(latency_ms) FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) AS max_latency_ms
        FROM samples
        GROUP BY model_key
        HAVING COUNT(*) FILTER (WHERE latency_ms IS NOT NULL AND latency_ms > 0) > 0
        ORDER BY calls DESC, model_key ASC
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

    metrics: list[dict[str, Any]] = []
    by_source: dict[str, list[dict[str, Any]]] = {}
    for source, rows in source_rows.items():
        items = [_metric_row(source, row) for row in rows]
        by_source[source] = items
        metrics.extend(items)

    total_calls = sum(item["calls"] for item in metrics)
    total_failed = sum(item["failed_calls"] for item in metrics)
    slowest_top5 = sorted(
        [item for item in metrics if item["p95_latency_ms"] is not None],
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
            "failure_rate_pct": _round_pct((total_failed * 100.0 / total_calls) if total_calls else 0),
            "slowest_top5": slowest_top5,
        },
        "sources": {
            "chat_final_response": "chat_messages.quality_details.response_duration_ms|duration_ms",
            "oauth_llm_api": "oauth_usage_log.duration_ms",
            "background_llm": "bg_llm_usage_log.latency_ms",
            "runner_cli_total": "pipeline_jobs started_at/created_at -> completed_at/updated_at elapsed",
            "runner_cli_phase_metrics": "pipeline_runner_events model_attempt/cli_process/first_output/completed events",
        },
        "runner_cli_phase_metrics": [_phase_metric_row(row) for row in runner_phase_rows],
        "recent_runner_cli_events": [_event_row(row) for row in recent_runner_event_rows],
        "metrics": by_source,
    }
