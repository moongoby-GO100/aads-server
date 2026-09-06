"""Read-only aggregation for common deployment observability."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger()

ACTIVE_STATUSES = ("running", "verifying", "syncing_standby")
QUEUED_STATUSES = ("queued", "awaiting_approval")
TERMINAL_PIPELINE_STATUSES = ("done", "error", "cancelled", "rejected_done")
PROJECTS = ("AADS", "GO100", "KIS", "SF", "NTV2", "NAS")
DEPLOY_STALL_SECONDS = max(
    120,
    int(os.getenv("AADS_DEPLOY_STATUS_STALL_SECONDS", "300") or "300"),
)


def _dict_rows(rows: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _seconds_since(value: Any, now: datetime) -> int | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((now - value.astimezone(timezone.utc)).total_seconds()))


async def _table_exists(conn: Any, name: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{name}"))


async def _load_deploy_runs(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = await conn.fetch(
        """
        SELECT dr.*,
               CASE WHEN dr.image_digest IS NOT NULL
                         AND dr.image_digest = dr.standby_digest THEN 'synced'
                    WHEN dr.standby_digest IS NULL THEN 'unknown'
                    ELSE 'mismatch' END AS bg_sync_status
        FROM deploy_runs dr
        WHERE dr.status = ANY($1::text[])
        ORDER BY COALESCE(dr.queue_position, 2147483647), dr.created_at
        """,
        list(ACTIVE_STATUSES + QUEUED_STATUSES),
    )
    active = [dict(row) for row in rows if row["status"] in ACTIVE_STATUSES]
    queued = [dict(row) for row in rows if row["status"] in QUEUED_STATUSES]
    return active, queued


def _annotate_active_runs(active: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    for row in active:
        heartbeat_age = _seconds_since(row.get("last_heartbeat_at") or row.get("updated_at"), now)
        phase_age = _seconds_since(row.get("phase_started_at"), now)
        row["heartbeat_age_seconds"] = heartbeat_age
        row["phase_elapsed_seconds"] = phase_age
        row["stalled"] = bool(
            heartbeat_age is not None and heartbeat_age >= DEPLOY_STALL_SECONDS
        )
        row["effective_status"] = "stalled" if row["stalled"] else row.get("status")
        if row["stalled"]:
            row["signal"] = "deploy_phase_stalled"
            row["reconcile_action"] = "deploy_sh_reconcile_before_next_release"
            row["requires_ceo_approval"] = False
    return active


async def _load_recent_durations(conn: Any) -> list[dict[str, Any]]:
    return _dict_rows(await conn.fetch(
        """
        SELECT project, sample_count, avg_duration_ms, p50_duration_ms,
               p90_duration_ms, last_completed_at,
               COALESCE(source, 'deploy_runs') AS source
        FROM deploy_recent_durations
        ORDER BY project
        """
    ))


async def _load_phase_timeline(conn: Any) -> list[dict[str, Any]]:
    return _dict_rows(await conn.fetch(
        """
        SELECT e.deploy_run_id, r.project, r.release_sha, e.phase, e.status,
               e.phase_started_at, e.phase_completed_at, e.duration_ms,
               e.estimated_remaining_ms, e.error_summary
        FROM deploy_phase_events e
        JOIN deploy_runs r ON r.id = e.deploy_run_id
        WHERE e.deploy_run_id IN (
            SELECT id FROM deploy_runs ORDER BY created_at DESC LIMIT 20
        )
        ORDER BY e.phase_started_at DESC, e.id DESC
        LIMIT 200
        """
    ))


async def _load_legacy(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Legacy rows are evidence only; an unmatched started row is never active."""
    stale = _dict_rows(await conn.fetch(
        """
        SELECT dh.id AS deploy_history_id, dh.project, dh.git_commit AS release_sha,
               dh.created_at, EXTRACT(EPOCH FROM (NOW() - dh.created_at))::bigint AS age_seconds,
               'legacy_started_without_terminal_match' AS signal
        FROM deploy_history dh
        WHERE dh.status = 'started'
          AND dh.created_at < NOW() - INTERVAL '30 minutes'
          AND NOT EXISTS (
              SELECT 1 FROM deploy_history terminal
              WHERE terminal.project = dh.project
                AND terminal.git_commit IS NOT DISTINCT FROM dh.git_commit
                AND terminal.status IN ('success', 'failed', 'rolled_back', 'blocked')
                AND terminal.created_at >= dh.created_at
                AND terminal.created_at <= dh.created_at + INTERVAL '24 hours'
          )
        ORDER BY dh.created_at DESC
        LIMIT 100
        """
    ))
    durations = _dict_rows(await conn.fetch(
        """
        SELECT project, COUNT(*)::int AS sample_count,
               ROUND(AVG(duration_s) * 1000)::bigint AS avg_duration_ms,
               ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s) * 1000)::bigint AS p50_duration_ms,
               ROUND(percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_s) * 1000)::bigint AS p90_duration_ms,
               MAX(COALESCE(finished_at, created_at)) AS last_completed_at,
               'deploy_history' AS source
        FROM deploy_history
        WHERE status = 'success' AND duration_s IS NOT NULL
          AND created_at >= NOW() - INTERVAL '90 days'
        GROUP BY project ORDER BY project
        """
    ))
    return stale, durations


async def _load_runner_signals(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = _dict_rows(await conn.fetch(
        """
        SELECT job_id AS runner_job_id, project, status, phase, commit_hash AS release_sha,
               runner_pid, error_detail, started_at, created_at, updated_at,
               EXTRACT(EPOCH FROM (NOW() - updated_at))::bigint AS idle_seconds
        FROM pipeline_jobs
        WHERE project = ANY($1::text[])
          AND status <> ALL($2::text[])
        ORDER BY created_at
        LIMIT 300
        """,
        list(PROJECTS), list(TERMINAL_PIPELINE_STATUSES),
    ))
    queue = []
    signals = []
    for row in rows:
        status = str(row.get("status") or "")
        project = str(row.get("project") or "").upper()
        idle = int(row.get("idle_seconds") or 0)
        detail = str(row.get("error_detail") or "").lower()
        if status in QUEUED_STATUSES or status in ("pending_ceo_approval", "review_hold"):
            queue.append(row)
        signal = None
        if "process_died" in detail:
            signal = "process_died"
        elif project in ("GO100", "KIS") and status == "running" and idle >= 1800:
            signal = "zombie_candidate" if row.get("runner_pid") else "stale_running"
        elif project in ("GO100", "KIS") and status in ("awaiting_approval", "pending_ceo_approval") and idle >= 86400:
            signal = "stale_awaiting_approval"
        if signal:
            row["signal"] = signal
            row["reconcile_action"] = "review_only"
            row["requires_ceo_approval"] = True
            signals.append(row)
    for position, row in enumerate(queue, 1):
        row["queue_position"] = position
    return queue, signals


async def get_deploy_status(conn: Any) -> dict[str, Any]:
    """Return a stable response even while migration/data sources are unavailable."""
    now = datetime.now(timezone.utc)
    response: dict[str, Any] = {
        "generated_at": now,
        "schema_version": "deploy-observability-v1",
        "degraded": False,
        "degraded_reasons": [],
        "active_deployments": [],
        "queued_deployments": [],
        "recent_durations_per_project": [],
        "phase_timeline": [],
        "stale_zombie_signals": [],
        "legacy_stale_candidates": [],
        "bg_digest_sync": [],
        "next_deploy_readiness": {"ready": True, "blockers": []},
    }

    has_runs = await _table_exists(conn, "deploy_runs")
    has_history = await _table_exists(conn, "deploy_history")
    has_pipeline = await _table_exists(conn, "pipeline_jobs")

    if has_runs:
        active, queued = await _load_deploy_runs(conn)
        active = _annotate_active_runs(active, now)
        response["active_deployments"] = active
        response["queued_deployments"] = queued
        response["recent_durations_per_project"] = await _load_recent_durations(conn)
        response["phase_timeline"] = await _load_phase_timeline(conn)
        response["bg_digest_sync"] = [
            {key: item.get(key) for key in (
                "id", "project", "release_sha", "current_slot", "candidate_slot",
                "image_digest", "standby_digest", "bg_sync_status",
            )}
            for item in active
        ]
    else:
        response["degraded"] = True
        response["degraded_reasons"].append("deploy_observability_migration_not_applied")

    if has_history:
        stale, legacy_durations = await _load_legacy(conn)
        response["legacy_stale_candidates"] = stale
        if not response["recent_durations_per_project"]:
            response["recent_durations_per_project"] = legacy_durations
    else:
        response["degraded"] = True
        response["degraded_reasons"].append("deploy_history_unavailable")

    if has_pipeline:
        runner_queue, signals = await _load_runner_signals(conn)
        known_jobs = {item.get("runner_job_id") for item in response["queued_deployments"]}
        response["queued_deployments"].extend(
            item for item in runner_queue if item.get("runner_job_id") not in known_jobs
        )
        response["stale_zombie_signals"] = signals
    else:
        response["degraded"] = True
        response["degraded_reasons"].append("pipeline_jobs_unavailable")

    blockers = []
    live_active_deployments = [
        item for item in response["active_deployments"] if not item.get("stalled")
    ]
    stalled_active_deployments = [
        item for item in response["active_deployments"] if item.get("stalled")
    ]
    if live_active_deployments:
        blockers.append("deployment_in_progress")
    if stalled_active_deployments:
        blockers.append("deployment_reconciliation_required")
    if response["stale_zombie_signals"]:
        blockers.append("runner_reconciliation_required")
    if any(item.get("bg_sync_status") == "mismatch" for item in response["active_deployments"]):
        blockers.append("blue_green_digest_mismatch")
    response["next_deploy_readiness"] = {
        "ready": not blockers,
        "blockers": blockers,
        "next_queued_runner_job_id": (
            response["queued_deployments"][0].get("runner_job_id")
            if response["queued_deployments"] else None
        ),
    }
    return response
