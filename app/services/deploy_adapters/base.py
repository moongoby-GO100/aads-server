"""Common contracts for unified deployment adapters (AADS-UNIFIED-DEPLOY v1).

Every deployable target (AADS API, AADS Dashboard, GO100 backend/frontend, ...)
is represented by an adapter that knows:

* how to preflight a release SHA,
* how to start the rollout without blocking the caller,
* how to poll / verify the rollout from the central ``deploy_runs`` ledger,
* who owns the actual execution (central worker vs. project-owned runner).

The adapters intentionally never block the HTTP request: ``start()`` only kicks
a detached host worker and returns immediately, matching FR-003 of
``docs/reports/20260908_unified_deployment_management_prd.md``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger()

DEFAULT_TARGET_ENV = "production"
DEFAULT_START_TIMEOUT_SECONDS = 8

# Execution ownership
OWNER_CENTRAL_WORKER = "central_worker"
OWNER_PROJECT_RUNNER = "project_runner"


@dataclass
class DeployRequest:
    """Normalized deploy request handed to an adapter."""

    project: str
    component: str
    deploy_type: str
    release_sha: str
    target_env: str = DEFAULT_TARGET_ENV
    deploy_run_id: int | None = None
    requested_by: str = "ops"
    request_source: str = "ops_api"
    metadata: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str, str]:
        return (self.project.upper(), self.component.lower(), self.target_env.lower())


@dataclass
class PreflightResult:
    ok: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "details": dict(self.details),
        }


@dataclass
class DeployStartResult:
    started: bool
    status: str
    detail: str = ""
    ownership: str = OWNER_CENTRAL_WORKER
    returncode: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "status": self.status,
            "detail": self.detail[-500:] if self.detail else "",
            "ownership": self.ownership,
            "returncode": self.returncode,
        }


@dataclass
class DeployPollResult:
    status: str
    phase: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "phase": self.phase, "details": dict(self.details)}


@dataclass
class VerifyResult:
    ok: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": list(self.checks), "detail": self.detail}


@dataclass
class RollbackResult:
    supported: bool
    performed: bool = False
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"supported": self.supported, "performed": self.performed, "detail": self.detail}


@runtime_checkable
class DeployAdapter(Protocol):
    project: str
    component: str
    deploy_type: str

    async def preflight(self, request: DeployRequest) -> PreflightResult: ...

    async def start(self, request: DeployRequest) -> DeployStartResult: ...

    async def poll(self, conn: Any, deploy_run_id: int) -> DeployPollResult: ...

    async def verify(self, conn: Any, deploy_run_id: int) -> VerifyResult: ...

    async def rollback(self, conn: Any, deploy_run_id: int) -> RollbackResult: ...


class BaseDeployAdapter:
    """Shared behaviour: ledger-backed poll/verify plus launcher helpers."""

    project: str = "AADS"
    component: str = "api"
    deploy_type: str = "api_bluegreen"
    ownership: str = OWNER_CENTRAL_WORKER
    launcher_candidates: tuple[str, ...] = ()
    launcher_args: tuple[str, ...] = ()
    health_url: str | None = None
    route_url: str | None = None
    supports_rollback: bool = False
    start_timeout_seconds: int = DEFAULT_START_TIMEOUT_SECONDS

    # ---- descriptive ---------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "adapter": type(self).__name__,
            "project": self.project,
            "component": self.component,
            "deploy_type": self.deploy_type,
            "ownership": self.ownership,
            "launcher": self.resolve_launcher_path(),
            "launcher_available": self.resolve_launcher_path() is not None,
            "health_url": self.health_url,
            "route_url": self.route_url,
            "supports_rollback": self.supports_rollback,
        }

    def resolve_launcher_path(self) -> str | None:
        for candidate in self.launcher_candidates:
            try:
                if candidate and Path(candidate).exists():
                    return candidate
            except OSError:  # pragma: no cover - defensive
                continue
        return None

    # ---- lifecycle -----------------------------------------------------
    async def preflight(self, request: DeployRequest) -> PreflightResult:
        from app.services.deploy_observability import git_release_preflight

        details = git_release_preflight(self.project, request.release_sha)
        blockers: list[str] = []
        warnings: list[str] = []
        if details.get("repo_path") is None:
            warnings.append("repo_path_unavailable_in_container")
        elif details.get("release_known") is False:
            blockers.append(f"release_sha_not_found_in_repo:{request.release_sha}")
        if details.get("dirty_files"):
            warnings.append(f"repo_dirty_files:{len(details['dirty_files'])}")
        if self.ownership == OWNER_CENTRAL_WORKER and self.resolve_launcher_path() is None:
            blockers.append("deploy_worker_launcher_unavailable")
        return PreflightResult(ok=not blockers, blockers=blockers, warnings=warnings, details=details)

    async def start(self, request: DeployRequest) -> DeployStartResult:
        launcher = self.resolve_launcher_path()
        if launcher is None:
            return DeployStartResult(
                started=False,
                status="launcher_unavailable",
                detail=f"no launcher found for {self.project}/{self.component}",
                ownership=self.ownership,
            )
        args = [
            "bash",
            launcher,
            *self.launcher_args,
            request.release_sha,
            str(request.deploy_run_id or 0),
            request.request_source or "ops_api",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.start_timeout_seconds)
        except asyncio.TimeoutError:
            return DeployStartResult(
                started=True,
                status="start_timeout_assumed_background",
                detail="launcher did not return in time; check /api/v1/ops/deploy/status",
                ownership=self.ownership,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("deploy_adapter_start_failed", adapter=type(self).__name__, error=str(exc))
            return DeployStartResult(
                started=False, status="start_failed", detail=str(exc), ownership=self.ownership
            )

        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        error = (stderr or b"").decode("utf-8", errors="replace").strip()
        lowered = output.lower()
        if "deferred_to_host_drain" in lowered:
            return DeployStartResult(
                started=True,
                status="deferred_to_host_drain",
                detail=(output or error),
                ownership=self.ownership,
                returncode=proc.returncode,
            )
        if "queue empty" in lowered:
            return DeployStartResult(
                started=False,
                status="worker_saw_empty_queue",
                detail=(output or error),
                ownership=self.ownership,
                returncode=proc.returncode,
            )
        started = proc.returncode == 0 and ("started" in lowered or "already running" in lowered)
        return DeployStartResult(
            started=started,
            status="started" if started else "start_failed",
            detail=(output or error),
            ownership=self.ownership,
            returncode=proc.returncode,
        )

    async def poll(self, conn: Any, deploy_run_id: int) -> DeployPollResult:
        row = await conn.fetchrow(
            """
            SELECT id, project, component, status, phase, release_sha,
                   phase_started_at, phase_completed_at, last_heartbeat_at,
                   error_summary
              FROM deploy_runs
             WHERE id = $1
            """,
            int(deploy_run_id),
        )
        if row is None:
            return DeployPollResult(status="unknown", phase=None, details={"reason": "deploy_run_not_found"})
        data = dict(row)
        return DeployPollResult(
            status=str(data.get("status") or "unknown"),
            phase=data.get("phase"),
            details={k: v for k, v in data.items() if k not in ("status", "phase")},
        )

    async def verify(self, conn: Any, deploy_run_id: int) -> VerifyResult:
        poll = await self.poll(conn, deploy_run_id)
        checks = [
            {
                "name": "ledger_status",
                "ok": poll.status in ("success", "verified"),
                "value": poll.status,
            }
        ]
        events = await conn.fetch(
            """
            SELECT phase, status, duration_ms, error_summary
              FROM deploy_phase_events
             WHERE deploy_run_id = $1
             ORDER BY id DESC
             LIMIT 20
            """,
            int(deploy_run_id),
        )
        failed = [dict(e) for e in events if str(dict(e).get("status")) == "failed"]
        checks.append({"name": "failed_phase_events", "ok": not failed, "value": len(failed)})
        ok = all(c["ok"] for c in checks)
        return VerifyResult(ok=ok, checks=checks, detail=poll.details.get("error_summary") or "")

    async def rollback(self, conn: Any, deploy_run_id: int) -> RollbackResult:
        return RollbackResult(
            supported=self.supports_rollback,
            performed=False,
            detail=(
                "rollback is handled by the project deploy script; use its own rollback path"
                if self.supports_rollback
                else "adapter does not implement automated rollback"
            ),
        )
