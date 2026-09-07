"""Aggregation and queue helpers for common deployment observability."""

from __future__ import annotations

import os
import re
import json
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
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
_RELEASE_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

PROJECT_REPO_PATHS = {
    "AADS": (
        os.getenv("AADS_SERVER_REPO_PATH", ""),
        "/app",
        "/root/aads/aads-server",
    ),
    "GO100": (
        os.getenv("GO100_REPO_PATH", ""),
        "/root/kis-autotrade-v4",
    ),
    "KIS": (
        os.getenv("KIS_REPO_PATH", ""),
        "/root/kis-autotrade-v4",
    ),
    "SF": (
        os.getenv("SF_REPO_PATH", ""),
        "/data/shortflow",
    ),
    "NTV2": (
        os.getenv("NTV2_REPO_PATH", ""),
        "/var/www/newtalk",
    ),
}


def _normalize_project(project: str) -> str:
    value = (project or "").strip().upper()
    if value not in PROJECTS:
        raise ValueError(f"unsupported project: {project}")
    return value


def _normalize_release_sha(release_sha: str) -> str:
    value = (release_sha or "").strip()
    if not value:
        raise ValueError("release_sha is required")
    if not _RELEASE_SHA_RE.match(value):
        raise ValueError("release_sha must be a 7-40 character git SHA")
    return value.lower()


def _dict_rows(rows: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _seconds_since(value: Any, now: datetime) -> int | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((now - value.astimezone(timezone.utc)).total_seconds()))


def _coerce_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _payload_release_metadata(row: dict[str, Any]) -> dict[str, Any]:
    payload = _coerce_payload(row.get("request_payload"))
    title = (
        payload.get("title")
        or payload.get("summary")
        or payload.get("reason")
        or payload.get("description")
        or ""
    )
    changed_files = payload.get("changed_files") or payload.get("files") or []
    if isinstance(changed_files, str):
        changed_files = [changed_files]
    if not isinstance(changed_files, list):
        changed_files = []
    normalized_files = [str(item) for item in changed_files if str(item or "").strip()]
    return {
        "release_title": str(title).strip()[:180] or None,
        "release_summary": str(title).strip()[:240] or None,
        "changed_files": normalized_files[:12],
        "changed_file_count": len(normalized_files),
    }


def _repo_paths_for_project(project: str) -> tuple[str, ...]:
    raw_paths = PROJECT_REPO_PATHS.get((project or "").upper(), ())
    paths: list[str] = []
    for raw in raw_paths:
        if raw and raw not in paths:
            paths.append(raw)
    return tuple(paths)


def _git_output(repo: str, args: list[str]) -> str | None:
    path = Path(repo)
    if not path.exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    return completed.stdout.strip()


@lru_cache(maxsize=256)
def _git_release_metadata(project: str, release_sha: str) -> dict[str, Any]:
    sha = (release_sha or "").strip()
    if not sha:
        return {}
    for repo in _repo_paths_for_project(project):
        if _git_output(repo, ["cat-file", "-e", f"{sha}^{{commit}}"]) is None:
            continue
        title = _git_output(repo, ["log", "-1", "--pretty=%s", sha]) or ""
        body = _git_output(repo, ["log", "-1", "--pretty=%b", sha]) or ""
        files_raw = _git_output(repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", sha]) or ""
        files = [line.strip() for line in files_raw.splitlines() if line.strip()]
        return {
            "release_title": title[:180] or None,
            "release_summary": (body.splitlines()[0].strip() if body.strip() else title)[:240] or None,
            "changed_files": files[:12],
            "changed_file_count": len(files),
        }
    return {}


def _apply_release_metadata(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        payload_meta = _payload_release_metadata(row)
        git_meta = _git_release_metadata(
            str(row.get("project") or ""),
            str(row.get("release_sha") or ""),
        )
        merged = {**payload_meta, **{k: v for k, v in git_meta.items() if v not in (None, "", [])}}
        row.update(merged)
    return rows


def _apply_deploy_time_aliases(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    terminal_statuses = {"completed", "success", "failed", "error", "blocked", "superseded", "cancelled"}
    for row in rows:
        row.setdefault(
            "started_at",
            row.get("requested_at")
            or row.get("created_at")
            or row.get("phase_started_at")
            or row.get("updated_at"),
        )
        status = str(row.get("status") or "").lower()
        row.setdefault(
            "completed_at",
            (
                row.get("phase_completed_at")
                or row.get("updated_at")
            ) if status in terminal_statuses else None,
        )
    return rows


async def _table_exists(conn: Any, name: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{name}"))


async def enqueue_deploy_request(
    conn: Any,
    *,
    project: str,
    release_sha: str,
    runner_job_id: str | None = None,
    requested_by: str = "ops",
    request_source: str = "ops_api",
    commit_status: str = "committed",
    push_status: str = "pushed",
    auto_start: bool = True,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue a deploy in the ops DB without blocking the caller for the rollout."""
    project_key = _normalize_project(project)
    release = _normalize_release_sha(release_sha)
    source = (request_source or "ops_api").strip()[:80]
    actor = (requested_by or "ops").strip()[:120]
    commit_state = (commit_status or "committed").strip()[:40]
    push_state = (push_status or "pushed").strip()[:40]
    payload = dict(metadata or {})

    async with conn.transaction():
        existing = await conn.fetchrow(
            """
            SELECT *
              FROM deploy_runs
             WHERE project = $1
               AND release_sha = $2
               AND status IN ('queued', 'awaiting_approval', 'running', 'verifying', 'syncing_standby')
             ORDER BY created_at DESC, id DESC
             LIMIT 1
             FOR UPDATE
            """,
            project_key,
            release,
        )
        if existing:
            return {**dict(existing), "deduplicated": True}

        await conn.execute(
            """
            UPDATE deploy_runs
               SET status = 'superseded',
                   phase = 'superseded_by_newer_deploy_request',
                   phase_completed_at = NOW(),
                   updated_at = NOW(),
                   error_summary = CONCAT_WS('; ', NULLIF(error_summary, ''), $3)
             WHERE project = $1
               AND status = 'queued'
               AND phase = 'queued_for_deploy'
               AND release_sha IS DISTINCT FROM $2
            """,
            project_key,
            release,
            f"superseded by newer queued release {release}",
        )

        queue_position = await conn.fetchval(
            """
            SELECT COALESCE(MAX(queue_position), 0) + 1
              FROM deploy_runs
             WHERE project = $1
               AND status = 'queued'
               AND phase = 'queued_for_deploy'
            """,
            project_key,
        )
        row = await conn.fetchrow(
            """
            INSERT INTO deploy_runs(
                project, release_sha, runner_job_id, status, phase, phase_started_at,
                queue_position, error_summary, requested_by, request_source,
                commit_status, push_status, auto_start, request_payload,
                requested_at, last_heartbeat_at, created_at, updated_at
            )
            VALUES(
                $1, $2, NULLIF($3, ''), 'queued', 'queued_for_deploy', NOW(),
                $4, 'queued by ops deploy request API', $5, $6,
                $7, $8, $9, $10::jsonb,
                NOW(), NOW(), NOW(), NOW()
            )
            RETURNING *
            """,
            project_key,
            release,
            (runner_job_id or "").strip(),
            int(queue_position or 1),
            actor,
            source,
            commit_state,
            push_state,
            bool(auto_start),
            json.dumps(payload, ensure_ascii=False, default=str),
        )
    return {**dict(row), "deduplicated": False}


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
    active = _apply_deploy_time_aliases(_apply_release_metadata([dict(row) for row in rows if row["status"] in ACTIVE_STATUSES]))
    queued = _apply_deploy_time_aliases(_apply_release_metadata([dict(row) for row in rows if row["status"] in QUEUED_STATUSES]))
    return active, queued


async def _load_recent_completed_deployments(conn: Any) -> list[dict[str, Any]]:
    rows = _dict_rows(await conn.fetch(
        """
        SELECT dr.*,
               CASE WHEN dr.image_digest IS NOT NULL
                         AND dr.image_digest = dr.standby_digest THEN 'synced'
                    WHEN dr.standby_digest IS NULL THEN 'unknown'
                    ELSE 'mismatch' END AS bg_sync_status
        FROM deploy_runs dr
        WHERE dr.status IN ('completed', 'success')
        ORDER BY COALESCE(dr.phase_completed_at, dr.updated_at, dr.created_at) DESC, dr.id DESC
        LIMIT 12
        """
    ))
    return _apply_deploy_time_aliases(_apply_release_metadata(rows))


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
        "recent_completed_deployments": [],
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
        response["recent_completed_deployments"] = await _load_recent_completed_deployments(conn)
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
