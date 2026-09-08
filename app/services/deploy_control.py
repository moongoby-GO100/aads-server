"""Manual control plane for unified deployments (approve/cancel/retry/reconcile/logs).

Implements PRD section 9.3 of
``docs/reports/20260908_unified_deployment_management_prd.md``.

Safety rules:
* ``reconcile`` defaults to dry-run; applying requires an explicit flag.
* Only non-terminal runs can be cancelled; only terminal runs can be retried.
* Every state change writes a ``deploy_phase_events`` audit row.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

ACTIVE_STATUSES = ("running", "verifying", "syncing_standby")
QUEUED_STATUSES = ("queued", "awaiting_approval")
TERMINAL_STATUSES = ("success", "failed", "blocked", "cancelled", "superseded")

STALE_HEARTBEAT_SECONDS = max(
    120, int(os.getenv("AADS_DEPLOY_STALE_AFTER_SECONDS", "600") or "600")
)
STALE_QUEUE_SECONDS = max(
    600, int(os.getenv("AADS_DEPLOY_STALE_QUEUE_SECONDS", "3600") or "3600")
)


async def _fetch_run(conn: Any, run_id: int) -> dict[str, Any] | None:
    row = await conn.fetchrow("SELECT * FROM deploy_runs WHERE id = $1", int(run_id))
    return dict(row) if row else None


async def _audit_phase_event(
    conn: Any,
    run_id: int,
    *,
    phase: str,
    status: str,
    detail: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO deploy_phase_events(
            deploy_run_id, phase, status, phase_started_at, phase_completed_at,
            duration_ms, error_summary, metadata
        )
        VALUES($1, $2, $3, NOW(), NOW(), 0, $4, $5::jsonb)
        """,
        int(run_id),
        phase[:120],
        status[:40],
        detail[:500],
        json.dumps(metadata or {}, ensure_ascii=False, default=str),
    )


async def cancel_deploy_run(
    conn: Any, run_id: int, *, actor: str = "ops", reason: str = ""
) -> dict[str, Any]:
    """Cancel a queued / awaiting_approval run. Active rollouts are never killed here."""
    run = await _fetch_run(conn, run_id)
    if run is None:
        return {"ok": False, "status": "not_found", "detail": f"deploy_run {run_id} not found"}
    status = str(run.get("status") or "")
    if status in TERMINAL_STATUSES:
        return {"ok": False, "status": "already_terminal", "current_status": status}
    if status in ACTIVE_STATUSES:
        return {
            "ok": False,
            "status": "active_rollout_not_cancellable",
            "current_status": status,
            "detail": "an in-flight rollout must be stopped by its deploy script, not the API",
        }
    detail = f"cancelled by {actor}" + (f": {reason}" if reason else "")
    async with conn.transaction():
        await conn.execute(
            """
            UPDATE deploy_runs
               SET status = 'cancelled',
                   phase = 'cancelled_by_operator',
                   phase_completed_at = NOW(),
                   updated_at = NOW(),
                   error_summary = CONCAT_WS('; ', NULLIF(error_summary, ''), $2)
             WHERE id = $1
            """,
            int(run_id),
            detail,
        )
        await conn.execute(
            """
            UPDATE deploy_components
               SET status = 'cancelled', phase = 'cancelled_by_operator',
                   completed_at = NOW(), updated_at = NOW(),
                   error_summary = $2
             WHERE deploy_run_id = $1
               AND status NOT IN ('success', 'failed', 'cancelled')
            """,
            int(run_id),
            detail,
        )
        await _audit_phase_event(
            conn, run_id, phase="cancelled_by_operator", status="cancelled",
            detail=detail, metadata={"actor": actor, "reason": reason},
        )
    return {
        "ok": True,
        "status": "cancelled",
        "deploy_run_id": int(run_id),
        "project": run.get("project"),
        "component": run.get("component"),
        "release_sha": run.get("release_sha"),
        "detail": detail,
    }


async def approve_deploy_run(conn: Any, run_id: int, *, actor: str = "ceo") -> dict[str, Any]:
    """Move an ``awaiting_approval`` run into the normal queue."""
    run = await _fetch_run(conn, run_id)
    if run is None:
        return {"ok": False, "status": "not_found", "detail": f"deploy_run {run_id} not found"}
    status = str(run.get("status") or "")
    if status != "awaiting_approval":
        return {"ok": False, "status": "not_awaiting_approval", "current_status": status}
    detail = f"approved by {actor}"
    async with conn.transaction():
        await conn.execute(
            """
            UPDATE deploy_runs
               SET status = 'queued',
                   phase = 'queued_for_deploy',
                   phase_started_at = NOW(),
                   updated_at = NOW(),
                   approval_policy = 'manual_approved',
                   error_summary = CONCAT_WS('; ', NULLIF(error_summary, ''), $2)
             WHERE id = $1
            """,
            int(run_id),
            detail,
        )
        await _audit_phase_event(
            conn, run_id, phase="approved", status="queued", detail=detail,
            metadata={"actor": actor},
        )
    return {
        "ok": True,
        "status": "queued",
        "deploy_run_id": int(run_id),
        "project": run.get("project"),
        "component": run.get("component"),
        "release_sha": run.get("release_sha"),
        "detail": detail,
    }


async def retry_deploy_run(
    conn: Any, run_id: int, *, actor: str = "ops", auto_start: bool = True
) -> dict[str, Any]:
    """Re-queue a terminal (failed/blocked/cancelled/superseded) run as a new request."""
    from app.services.deploy_observability import enqueue_deploy_request

    run = await _fetch_run(conn, run_id)
    if run is None:
        return {"ok": False, "status": "not_found", "detail": f"deploy_run {run_id} not found"}
    status = str(run.get("status") or "")
    if status not in ("failed", "blocked", "cancelled", "superseded"):
        return {
            "ok": False,
            "status": "not_retryable",
            "current_status": status,
            "detail": "only failed/blocked/cancelled/superseded runs can be retried",
        }
    payload = run.get("request_payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    metadata = dict(payload or {})
    metadata["retry_of_deploy_run_id"] = int(run_id)
    metadata["retry_requested_by"] = actor
    row = await enqueue_deploy_request(
        conn,
        project=str(run.get("project") or "AADS"),
        release_sha=str(run.get("release_sha") or ""),
        component=str(run.get("component") or "api"),
        deploy_type=str(run.get("deploy_type") or "") or None,
        target_env=str(run.get("target_env") or "production"),
        runner_job_id=run.get("runner_job_id"),
        requested_by=actor,
        request_source="ops_api_retry",
        commit_status=str(run.get("commit_status") or "committed"),
        push_status=str(run.get("push_status") or "pushed"),
        auto_start=auto_start,
        rollback_plan=run.get("rollback_plan"),
        approval_policy=str(run.get("approval_policy") or "auto_if_green"),
        metadata=metadata,
    )
    await _audit_phase_event(
        conn, run_id, phase="retry_requested", status="superseded",
        detail=f"retried by {actor} as deploy_run {row.get('id')}",
        metadata={"actor": actor, "new_deploy_run_id": row.get("id")},
    )
    return {
        "ok": True,
        "status": "queued",
        "retry_of": int(run_id),
        "deploy_run_id": row.get("id"),
        "project": row.get("project"),
        "component": row.get("component"),
        "release_sha": row.get("release_sha"),
        "deduplicated": row.get("deduplicated", False),
    }


async def reconcile_deploy_state(
    conn: Any, *, dry_run: bool = True, actor: str = "ops"
) -> dict[str, Any]:
    """Detect (and optionally clear) stale runs, stuck queue entries and expired locks.

    ``dry_run=True`` (default) never mutates state. PID liveness cannot be
    checked from inside the API container, so heartbeat age is the signal.
    """
    stale_runs = [
        dict(r)
        for r in await conn.fetch(
            """
            SELECT id, project, component, release_sha, status, phase,
                   deploy_pid,
                   EXTRACT(EPOCH FROM (NOW() - COALESCE(last_heartbeat_at, updated_at)))::bigint
                       AS heartbeat_age_seconds
              FROM deploy_runs
             WHERE status = ANY($1::text[])
               AND COALESCE(last_heartbeat_at, updated_at) < NOW() - ($2 || ' seconds')::interval
             ORDER BY id
             LIMIT 50
            """,
            list(ACTIVE_STATUSES),
            str(STALE_HEARTBEAT_SECONDS),
        )
    ]
    stuck_queue = [
        dict(r)
        for r in await conn.fetch(
            """
            SELECT id, project, component, release_sha, status, phase,
                   EXTRACT(EPOCH FROM (NOW() - created_at))::bigint AS queued_age_seconds
              FROM deploy_runs
             WHERE status = 'queued'
               AND created_at < NOW() - ($1 || ' seconds')::interval
             ORDER BY id
             LIMIT 50
            """,
            str(STALE_QUEUE_SECONDS),
        )
    ]
    expired_locks: list[dict[str, Any]] = []
    lock_table = await conn.fetchval("SELECT to_regclass('public.deploy_locks')")
    if lock_table:
        expired_locks = [
            dict(r)
            for r in await conn.fetch(
                """
                SELECT project, component, target_env, deploy_run_id, owner_instance,
                       lease_expires_at, heartbeat_at
                  FROM deploy_locks
                 WHERE lease_expires_at IS NOT NULL
                   AND lease_expires_at < NOW()
                 ORDER BY project, component
                 LIMIT 50
                """
            )
        ]

    findings = {
        "stale_active_runs": stale_runs,
        "stuck_queued_runs": stuck_queue,
        "expired_locks": expired_locks,
        "pid_check": "unavailable_from_api_container",
        "stale_heartbeat_threshold_seconds": STALE_HEARTBEAT_SECONDS,
        "stuck_queue_threshold_seconds": STALE_QUEUE_SECONDS,
    }
    actions: list[dict[str, Any]] = []

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "findings": findings,
            "would_apply": {
                "fail_stale_runs": [r["id"] for r in stale_runs],
                "release_expired_locks": [
                    f"{r['project']}/{r['component']}/{r['target_env']}" for r in expired_locks
                ],
            },
            "actions": actions,
        }

    async with conn.transaction():
        for run in stale_runs:
            detail = (
                f"stale deploy reconciled by {actor}: "
                f"heartbeat_age={run.get('heartbeat_age_seconds')}s pid={run.get('deploy_pid')}"
            )
            await conn.execute(
                """
                UPDATE deploy_runs
                   SET status = 'failed',
                       phase_completed_at = NOW(),
                       updated_at = NOW(),
                       last_heartbeat_at = NOW(),
                       error_summary = CONCAT_WS('; ', NULLIF(error_summary, ''), $2)
                 WHERE id = $1
                   AND status = ANY($3::text[])
                """,
                int(run["id"]),
                detail,
                list(ACTIVE_STATUSES),
            )
            await conn.execute(
                """
                UPDATE deploy_components
                   SET status = 'failed', phase = 'reconciled_stale',
                       completed_at = NOW(), updated_at = NOW(), error_summary = $2
                 WHERE deploy_run_id = $1
                   AND status NOT IN ('success', 'failed', 'cancelled')
                """,
                int(run["id"]),
                detail,
            )
            await _audit_phase_event(
                conn, int(run["id"]), phase="reconciled_stale", status="failed",
                detail=detail, metadata={"actor": actor, "source": "ops_reconcile_api"},
            )
            actions.append({"action": "fail_stale_run", "deploy_run_id": int(run["id"])})

        for lock in expired_locks:
            await conn.execute(
                """
                DELETE FROM deploy_locks
                 WHERE project = $1 AND component = $2 AND target_env = $3
                   AND lease_expires_at IS NOT NULL AND lease_expires_at < NOW()
                """,
                lock["project"],
                lock["component"],
                lock["target_env"],
            )
            actions.append(
                {
                    "action": "release_expired_lock",
                    "target": f"{lock['project']}/{lock['component']}/{lock['target_env']}",
                }
            )

    logger.info("ops_deploy_reconcile_applied", actor=actor, actions=len(actions))
    return {"ok": True, "dry_run": False, "findings": findings, "actions": actions}


def _tail_file(path: str, max_lines: int = 200) -> list[str]:
    try:
        file_path = Path(path)
        if not file_path.exists() or not file_path.is_file():
            return []
        with file_path.open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in handle.readlines()[-max_lines:]]
    except Exception:  # pragma: no cover - defensive
        return []


async def get_deploy_run_logs(conn: Any, run_id: int, *, max_lines: int = 200) -> dict[str, Any]:
    """Phase timeline + component log tail for one deploy run."""
    run = await _fetch_run(conn, run_id)
    if run is None:
        return {"ok": False, "status": "not_found", "detail": f"deploy_run {run_id} not found"}
    events = [
        dict(r)
        for r in await conn.fetch(
            """
            SELECT id, phase, status, phase_started_at, phase_completed_at,
                   duration_ms, error_summary
              FROM deploy_phase_events
             WHERE deploy_run_id = $1
             ORDER BY id
            """,
            int(run_id),
        )
    ]
    components = [
        dict(r)
        for r in await conn.fetch(
            """
            SELECT component, deploy_type, status, phase, started_at, completed_at,
                   duration_ms, log_path, error_summary
              FROM deploy_components
             WHERE deploy_run_id = $1
             ORDER BY id
            """,
            int(run_id),
        )
    ]
    log_tails = []
    for component in components:
        log_path = component.get("log_path")
        if log_path:
            log_tails.append(
                {
                    "component": component.get("component"),
                    "log_path": log_path,
                    "lines": _tail_file(str(log_path), max_lines),
                }
            )
    return {
        "ok": True,
        "deploy_run_id": int(run_id),
        "project": run.get("project"),
        "component": run.get("component"),
        "release_sha": run.get("release_sha"),
        "status": run.get("status"),
        "phase": run.get("phase"),
        "started_at": run.get("requested_at") or run.get("created_at"),
        "completed_at": run.get("phase_completed_at"),
        "error_summary": run.get("error_summary"),
        "phase_events": events,
        "components": components,
        "log_tails": log_tails,
    }
