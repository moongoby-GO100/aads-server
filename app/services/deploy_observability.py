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
PROJECTS = ("AADS", "FOOD", "GO100", "KIS", "SF", "NTV2", "NAS")
DEFAULT_COMPONENT = "api"
DEFAULT_TARGET_ENV = "production"
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
    "FOOD": (
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


def _normalize_slug(value: str | None, *, default: str, field_name: str) -> str:
    normalized = (value or default).strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,79}$", normalized):
        raise ValueError(f"{field_name} must be 1-80 chars: lowercase letters, numbers, '-' or '_'")
    return normalized


def _default_deploy_type(project: str, component: str) -> str:
    if project == "FOOD" and component in ("store-assistant", "store_assistant"):
        return "docker_service_replace"
    if component in ("dashboard", "frontend"):
        return "dashboard_bluegreen"
    if component in ("docs", "static_docs"):
        return "static_docs_publish"
    if component in ("db", "migration"):
        return "db_migration"
    if component in ("config", "prompt"):
        return "config_prompt_release"
    if project in ("GO100", "KIS") and component == "backend":
        return "backend_graceful_reload"
    if project == "NTV2" and component == "app":
        return "php_optimize_reload"
    if component == "worker":
        return "worker_restart"
    return "api_bluegreen"


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


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return [stripped]
        return _coerce_string_list(parsed)
    return []


def _payload_release_metadata(row: dict[str, Any]) -> dict[str, Any]:
    payload = _coerce_payload(row.get("request_payload"))
    title = (
        payload.get("title")
        or payload.get("summary")
        or payload.get("reason")
        or payload.get("description")
        or ""
    )
    normalized_files = _coerce_string_list(payload.get("changed_files") or payload.get("files"))
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


def git_release_preflight(project: str, release_sha: str) -> dict[str, Any]:
    """Inspect the local repo for a release SHA before handing it to an adapter.

    Returns a dict that is safe to embed in API responses:
    ``repo_path`` is ``None`` when the repository is not visible from this
    process (e.g. the dashboard repo is not mounted into the API container),
    which is a warning rather than a blocker.
    """
    sha = (release_sha or "").strip()
    result: dict[str, Any] = {
        "project": (project or "").upper(),
        "release_sha": sha,
        "repo_path": None,
        "release_known": None,
        "head_sha": None,
        "dirty_files": [],
        "unpushed_commits": None,
    }
    for repo in _repo_paths_for_project(project):
        head = _git_output(repo, ["rev-parse", "--short=12", "HEAD"])
        if head is None:
            continue
        result["repo_path"] = repo
        result["head_sha"] = head
        result["release_known"] = bool(
            sha and _git_output(repo, ["cat-file", "-e", f"{sha}^{{commit}}"]) is not None
        )
        status_raw = _git_output(repo, ["status", "--porcelain"]) or ""
        result["dirty_files"] = [
            line.strip() for line in status_raw.splitlines() if line.strip()
        ][:20]
        ahead = _git_output(repo, ["rev-list", "--count", "@{upstream}..HEAD"])
        if ahead is not None and ahead.isdigit():
            result["unpushed_commits"] = int(ahead)
        break
    return result


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
    component: str = DEFAULT_COMPONENT,
    deploy_type: str | None = None,
    target_env: str = DEFAULT_TARGET_ENV,
    runner_job_id: str | None = None,
    requested_by: str = "ops",
    request_source: str = "ops_api",
    commit_status: str = "committed",
    push_status: str = "pushed",
    auto_start: bool = True,
    rollback_plan: str | None = None,
    approval_policy: str = "auto_if_green",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue a deploy in the ops DB without blocking the caller for the rollout."""
    project_key = _normalize_project(project)
    release = _normalize_release_sha(release_sha)
    component_key = _normalize_slug(component, default=DEFAULT_COMPONENT, field_name="component")
    env_key = _normalize_slug(target_env, default=DEFAULT_TARGET_ENV, field_name="target_env")
    deploy_type_key = _normalize_slug(
        deploy_type or _default_deploy_type(project_key, component_key),
        default=_default_deploy_type(project_key, component_key),
        field_name="deploy_type",
    )
    source = (request_source or "ops_api").strip()[:80]
    actor = (requested_by or "ops").strip()[:120]
    commit_state = (commit_status or "committed").strip()[:40]
    push_state = (push_status or "pushed").strip()[:40]
    payload = dict(metadata or {})
    payload.setdefault("component", component_key)
    payload.setdefault("deploy_type", deploy_type_key)
    payload.setdefault("target_env", env_key)
    release_title = (
        str(payload.get("title") or payload.get("summary") or payload.get("reason") or "").strip()[:180]
        or None
    )
    release_summary = (
        str(payload.get("summary") or payload.get("description") or payload.get("title") or "").strip()[:240]
        or None
    )
    normalized_files = _coerce_string_list(payload.get("changed_files") or payload.get("files"))

    async with conn.transaction():
        existing = await conn.fetchrow(
            """
            SELECT *
              FROM deploy_runs
             WHERE project = $1
               AND release_sha = $2
               AND component = $3
               AND target_env = $4
               AND status IN ('queued', 'awaiting_approval', 'running', 'verifying', 'syncing_standby')
             ORDER BY created_at DESC, id DESC
             LIMIT 1
             FOR UPDATE
            """,
            project_key,
            release,
            component_key,
            env_key,
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
                   error_summary = CONCAT_WS('; ', NULLIF(error_summary, ''), $3::text)
             WHERE project = $1
               AND component = $4
               AND target_env = $5
               AND status = 'queued'
               AND phase = 'queued_for_deploy'
               AND release_sha IS DISTINCT FROM $2
            """,
            project_key,
            release,
            f"superseded by newer queued release {release}",
            component_key,
            env_key,
        )

        queue_position = await conn.fetchval(
            """
            SELECT COALESCE(MAX(queue_position), 0) + 1
              FROM deploy_runs
             WHERE project = $1
               AND component = $2
               AND target_env = $3
               AND status = 'queued'
               AND phase = 'queued_for_deploy'
            """,
            project_key,
            component_key,
            env_key,
        )
        row = await conn.fetchrow(
            """
            INSERT INTO deploy_runs(
                project, component, deploy_type, target_env, release_sha, runner_job_id, status, phase, phase_started_at,
                queue_position, error_summary, requested_by, request_source,
                commit_status, push_status, auto_start, request_payload,
                release_title, release_summary, rollback_plan, approval_policy,
                requested_at, last_heartbeat_at, created_at, updated_at
            )
            VALUES(
                $1, $2, $3, $4, $5, NULLIF($6, ''), 'queued', 'queued_for_deploy', NOW(),
                $7, 'queued by ops deploy request API', $8, $9,
                $10, $11, $12, $13::jsonb,
                $14, $15, $16, $17,
                NOW(), NOW(), NOW(), NOW()
            )
            RETURNING *
            """,
            project_key,
            component_key,
            deploy_type_key,
            env_key,
            release,
            (runner_job_id or "").strip(),
            int(queue_position or 1),
            actor,
            source,
            commit_state,
            push_state,
            bool(auto_start),
            json.dumps(payload, ensure_ascii=False, default=str),
            release_title,
            release_summary,
            (rollback_plan or payload.get("rollback_plan") or "")[:500] or None,
            (approval_policy or "auto_if_green")[:80],
        )
        await conn.execute(
            """
            INSERT INTO deploy_components(
                deploy_run_id, project, component, deploy_type, release_sha,
                status, phase, started_at, metadata, created_at, updated_at
            )
            VALUES($1, $2, $3, $4, $5, 'queued', 'queued_for_deploy', NOW(), $6::jsonb, NOW(), NOW())
            """,
            row["id"],
            project_key,
            component_key,
            deploy_type_key,
            release,
            json.dumps(payload, ensure_ascii=False, default=str),
        )
        await conn.execute(
            """
            INSERT INTO deploy_release_manifests(
                deploy_run_id, project, component, target_env, release_sha,
                title, summary, changed_files, tests, commits, risk_flags, created_at
            )
            VALUES($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb, $11::jsonb, NOW())
            """,
            row["id"],
            project_key,
            component_key,
            env_key,
            release,
            release_title,
            release_summary,
            json.dumps(normalized_files, ensure_ascii=False),
            json.dumps(payload.get("tests") if isinstance(payload.get("tests"), list) else [], ensure_ascii=False, default=str),
            json.dumps(payload.get("commits") if isinstance(payload.get("commits"), list) else [], ensure_ascii=False, default=str),
            json.dumps(payload.get("risk_flags") if isinstance(payload.get("risk_flags"), list) else [], ensure_ascii=False, default=str),
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


async def _load_recent_deployments(conn: Any) -> list[dict[str, Any]]:
    """Return recent terminal runs, including failures that never became releases."""
    rows = _dict_rows(await conn.fetch(
        """
        /* recent_terminal_deploy_history */
        SELECT dr.*,
               CASE WHEN dr.image_digest IS NOT NULL
                         AND dr.image_digest = dr.standby_digest THEN 'synced'
                    WHEN dr.standby_digest IS NULL THEN 'unknown'
                    ELSE 'mismatch' END AS bg_sync_status
        FROM deploy_runs AS dr
        WHERE dr.status IN ('completed', 'success', 'failed', 'error',
                            'blocked', 'superseded', 'cancelled')
        ORDER BY COALESCE(dr.phase_completed_at, dr.updated_at, dr.created_at) DESC, dr.id DESC
        LIMIT 20
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


async def _load_project_deployments(
    conn: Any,
    *,
    has_runs: bool,
    has_pipeline: bool,
) -> list[dict[str, Any]]:
    """Summarize every project, including projects that only have runner history."""
    overview: dict[str, dict[str, Any]] = {
        project: {
            "project": project,
            "status": "unknown",
            "phase": None,
            "release_sha": None,
            "runner_job_id": None,
            "started_at": None,
            "completed_at": None,
            "updated_at": None,
            "last_deploy_at": None,
            "last_success_sha": None,
            "source": "none",
            "has_deploy_run": False,
            "has_pipeline_job": False,
            "is_active": False,
            "is_queued": False,
        }
        for project in PROJECTS
    }

    if has_runs:
        rows = _dict_rows(await conn.fetch(
            """
            /* project_deploy_overview_latest_runs */
            SELECT DISTINCT ON (upper(project))
                   id, upper(project) AS project,
                   status, phase, release_sha, requested_at, created_at,
                   phase_started_at, phase_completed_at, updated_at,
                   duration_ms, release_title, release_summary, request_payload
              FROM deploy_runs
             WHERE upper(project) = ANY($1::text[])
             ORDER BY upper(project),
                      COALESCE(updated_at, phase_completed_at, phase_started_at, created_at) DESC NULLS LAST,
                      id DESC
            """,
            list(PROJECTS),
        ))
        for row in _apply_deploy_time_aliases(_apply_release_metadata(rows)):
            project = str(row.get("project") or "").upper()
            if project not in overview:
                continue
            status = str(row.get("status") or "")
            overview[project].update({
                "status": status or "unknown",
                "phase": row.get("phase"),
                "release_sha": row.get("release_sha"),
                "started_at": row.get("started_at"),
                "completed_at": row.get("completed_at"),
                "updated_at": row.get("updated_at"),
                "last_deploy_at": row.get("completed_at") or row.get("updated_at"),
                "source": "deploy_runs",
                "has_deploy_run": True,
                "is_active": status in ACTIVE_STATUSES,
                "is_queued": status in QUEUED_STATUSES,
                "duration_ms": row.get("duration_ms"),
                "release_title": row.get("release_title"),
                "release_summary": row.get("release_summary"),
                "changed_files": row.get("changed_files"),
                "changed_file_count": row.get("changed_file_count"),
            })

    if has_pipeline:
        latest_rows = _dict_rows(await conn.fetch(
            """
            /* project_deploy_overview_latest_pipeline */
            SELECT DISTINCT ON (upper(project))
                   upper(project) AS project,
                   job_id AS runner_job_id,
                   status, phase, commit_hash AS release_sha,
                   created_at, started_at, completed_at, deployed_at, updated_at
              FROM pipeline_jobs
             WHERE upper(project) = ANY($1::text[])
             ORDER BY upper(project),
                      COALESCE(updated_at, completed_at, deployed_at, started_at, created_at) DESC NULLS LAST
            """,
            list(PROJECTS),
        ))
        for row in latest_rows:
            project = str(row.get("project") or "").upper()
            if project not in overview:
                continue
            current = overview[project]
            current["has_pipeline_job"] = True
            if current["is_active"] or current["is_queued"]:
                current["runner_job_id"] = current.get("runner_job_id") or row.get("runner_job_id")
                continue
            status = str(row.get("status") or "")
            current.update({
                "status": status or current.get("status") or "unknown",
                "phase": row.get("phase") or current.get("phase"),
                "release_sha": row.get("release_sha") or current.get("release_sha"),
                "runner_job_id": row.get("runner_job_id"),
                "started_at": row.get("started_at") or current.get("started_at"),
                "completed_at": row.get("completed_at") or row.get("deployed_at") or current.get("completed_at"),
                "updated_at": row.get("updated_at") or current.get("updated_at"),
                "last_deploy_at": row.get("deployed_at") or row.get("completed_at") or row.get("updated_at"),
                "source": "pipeline_jobs",
                "is_active": status in ACTIVE_STATUSES,
                "is_queued": status in QUEUED_STATUSES or status in ("pending_ceo_approval", "review_hold"),
            })

        success_rows = _dict_rows(await conn.fetch(
            """
            /* project_deploy_overview_latest_success_pipeline */
            SELECT DISTINCT ON (upper(project))
                   upper(project) AS project,
                   commit_hash AS release_sha,
                   COALESCE(deployed_at, completed_at, updated_at, created_at) AS last_deploy_at
              FROM pipeline_jobs
             WHERE upper(project) = ANY($1::text[])
               AND status = 'done'
             ORDER BY upper(project),
                      COALESCE(deployed_at, completed_at, updated_at, created_at) DESC NULLS LAST
            """,
            list(PROJECTS),
        ))
        for row in success_rows:
            project = str(row.get("project") or "").upper()
            if project in overview:
                overview[project]["last_success_sha"] = row.get("release_sha")
                if not overview[project].get("last_deploy_at"):
                    overview[project]["last_deploy_at"] = row.get("last_deploy_at")

    return [overview[project] for project in PROJECTS]


async def _load_component_deployments(
    conn: Any,
    *,
    has_components: bool,
    has_runs: bool,
) -> list[dict[str, Any]]:
    """Return latest status per project/component, preferring the component ledger."""
    if has_components:
        rows = _dict_rows(await conn.fetch(
            """
            /* component_deploy_overview_latest_components */
            SELECT DISTINCT ON (upper(dc.project), dc.component, COALESCE(dc.metadata->>'target_env', 'production'))
                   dc.id AS component_id,
                   dc.deploy_run_id AS id,
                   upper(dc.project) AS project,
                   dc.component,
                   dc.deploy_type,
                   COALESCE(dc.metadata->>'target_env', 'production') AS target_env,
                   dc.release_sha,
                   dc.status,
                   dc.phase,
                   dc.started_at,
                   dc.completed_at,
                   dc.updated_at,
                   dc.duration_ms,
                   dc.health_url,
                   dc.route_url,
                   dc.image_digest,
                   dc.standby_digest,
                   drm.title AS release_title,
                   drm.summary AS release_summary,
                   drm.changed_files,
                   jsonb_array_length(COALESCE(drm.changed_files, '[]'::jsonb)) AS changed_file_count,
                   'deploy_components' AS source
              FROM deploy_components dc
              LEFT JOIN deploy_release_manifests drm
                ON drm.deploy_run_id = dc.deploy_run_id
               AND drm.component = dc.component
             WHERE upper(dc.project) = ANY($1::text[])
             ORDER BY upper(dc.project), dc.component, COALESCE(dc.metadata->>'target_env', 'production'),
                      COALESCE(dc.updated_at, dc.completed_at, dc.started_at, dc.created_at) DESC NULLS LAST,
                      dc.id DESC
            """,
            list(PROJECTS),
        ))
        for row in rows:
            row["changed_files"] = _coerce_string_list(row.get("changed_files"))
            status = str(row.get("status") or "")
            row["is_active"] = status in ACTIVE_STATUSES
            row["is_queued"] = status in QUEUED_STATUSES
        if rows:
            return rows

    if not has_runs:
        return []

    rows = _dict_rows(await conn.fetch(
        """
        /* component_deploy_overview_latest_runs */
        SELECT DISTINCT ON (upper(project), COALESCE(component, 'api'), COALESCE(target_env, 'production'))
               id,
               upper(project) AS project,
               COALESCE(component, 'api') AS component,
               COALESCE(deploy_type, 'api_bluegreen') AS deploy_type,
               COALESCE(target_env, 'production') AS target_env,
               release_sha,
               status,
               phase,
               requested_at,
               phase_started_at AS started_at,
               phase_completed_at AS completed_at,
               updated_at,
               duration_ms,
               release_title,
               release_summary,
               CASE
                   WHEN jsonb_typeof(request_payload->'changed_files') = 'array' THEN request_payload->'changed_files'
                   WHEN jsonb_typeof(request_payload->'files') = 'array' THEN request_payload->'files'
                   ELSE '[]'::jsonb
               END AS changed_files,
               jsonb_array_length(
                   CASE
                       WHEN jsonb_typeof(request_payload->'changed_files') = 'array' THEN request_payload->'changed_files'
                       WHEN jsonb_typeof(request_payload->'files') = 'array' THEN request_payload->'files'
                       ELSE '[]'::jsonb
                   END
               ) AS changed_file_count,
               'deploy_runs' AS source
          FROM deploy_runs
         WHERE upper(project) = ANY($1::text[])
         ORDER BY upper(project), COALESCE(component, 'api'), COALESCE(target_env, 'production'),
                  COALESCE(updated_at, phase_completed_at, phase_started_at, created_at) DESC NULLS LAST,
                  id DESC
        """,
        list(PROJECTS),
    ))
    for row in _apply_deploy_time_aliases(rows):
        row["changed_files"] = _coerce_string_list(row.get("changed_files"))
        status = str(row.get("status") or "")
        row["is_active"] = status in ACTIVE_STATUSES
        row["is_queued"] = status in QUEUED_STATUSES
    return rows


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
        "recent_deployments": [],
        "recent_durations_per_project": [],
        "phase_timeline": [],
        "stale_zombie_signals": [],
        "legacy_stale_candidates": [],
        "bg_digest_sync": [],
        "project_deployments": [],
        "component_deployments": [],
        "next_deploy_readiness": {"ready": True, "blockers": []},
    }

    has_runs = await _table_exists(conn, "deploy_runs")
    has_history = await _table_exists(conn, "deploy_history")
    has_pipeline = await _table_exists(conn, "pipeline_jobs")
    has_components = await _table_exists(conn, "deploy_components")

    if has_runs:
        active, queued = await _load_deploy_runs(conn)
        active = _annotate_active_runs(active, now)
        response["active_deployments"] = active
        response["queued_deployments"] = queued
        response["recent_completed_deployments"] = await _load_recent_completed_deployments(conn)
        response["recent_deployments"] = await _load_recent_deployments(conn)
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

    response["project_deployments"] = await _load_project_deployments(
        conn,
        has_runs=has_runs,
        has_pipeline=has_pipeline,
    )
    response["component_deployments"] = await _load_component_deployments(
        conn,
        has_components=has_components,
        has_runs=has_runs,
    )

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
