"""AADS-owned deploy adapters (API blue/green, Dashboard blue/green, docs publish)."""

from __future__ import annotations

from typing import Any

from app.services.deploy_adapters.base import (
    OWNER_CENTRAL_WORKER,
    BaseDeployAdapter,
    DeployRequest,
    DeployStartResult,
    PreflightResult,
)


class AadsApiBlueGreenAdapter(BaseDeployAdapter):
    """`deploy.sh bluegreen` on the host, claimed from the ops DB queue."""

    project = "AADS"
    component = "api"
    deploy_type = "api_bluegreen"
    ownership = OWNER_CENTRAL_WORKER
    launcher_candidates = (
        "/root/aads/aads-server/scripts/start_aads_deploy_queue_worker.sh",
        "/app/scripts/start_aads_deploy_queue_worker.sh",
    )
    launcher_args = ("bluegreen",)
    health_url = "https://aads.newtalk.kr/api/v1/ops/health-check"
    route_url = "https://aads.newtalk.kr"
    supports_rollback = True

    async def start(self, request: DeployRequest) -> DeployStartResult:
        # The legacy launcher takes (mode, trigger); it self-claims the newest
        # queued release from deploy_runs, so the release SHA is not passed.
        launcher = self.resolve_launcher_path()
        if launcher is None:
            return DeployStartResult(
                started=False,
                status="launcher_unavailable",
                detail="start_aads_deploy_queue_worker.sh not found",
                ownership=self.ownership,
            )
        import asyncio

        try:
            proc = await asyncio.create_subprocess_exec(
                "bash",
                launcher,
                "bluegreen",
                request.request_source or "ops_api",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.start_timeout_seconds)
        except asyncio.TimeoutError:
            return DeployStartResult(
                started=True,
                status="start_timeout_assumed_background",
                detail="worker launcher did not return within timeout; check /api/v1/ops/deploy/status",
                ownership=self.ownership,
            )
        except Exception as exc:  # pragma: no cover - defensive
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
        # "deploy queue empty" from inside the API container means the launcher
        # could not reach postgres (no docker CLI); that is NOT a started deploy.
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


class AadsDashboardBlueGreenAdapter(BaseDeployAdapter):
    """`/root/aads/aads-dashboard/deploy.sh` wrapped with central ledger sync."""

    project = "AADS"
    component = "dashboard"
    deploy_type = "dashboard_bluegreen"
    ownership = OWNER_CENTRAL_WORKER
    launcher_candidates = (
        "/root/aads/aads-server/scripts/start_aads_dashboard_deploy_worker.sh",
        "/app/scripts/start_aads_dashboard_deploy_worker.sh",
    )
    launcher_args = ()
    health_url = "https://aads.newtalk.kr/login"
    route_url = "https://aads.newtalk.kr"
    supports_rollback = True

    async def preflight(self, request: DeployRequest) -> PreflightResult:
        result = await super().preflight(request)
        # dashboard repo lives outside the API container; a missing repo path is
        # expected there and must not become a blocker.
        result.blockers = [b for b in result.blockers if not b.startswith("release_sha_not_found_in_repo")]
        result.details["repo"] = "/root/aads/aads-dashboard"
        result.ok = not result.blockers
        return result


class AadsDocsPublishAdapter(BaseDeployAdapter):
    """Docs are bind-mounted into the API container: publish == commit + mount check."""

    project = "AADS"
    component = "docs"
    deploy_type = "static_docs_publish"
    ownership = OWNER_CENTRAL_WORKER
    launcher_candidates = ()
    health_url = "https://aads.newtalk.kr/api/v1/project-docs/list"
    route_url = "https://aads.newtalk.kr/docs"
    supports_rollback = False

    async def preflight(self, request: DeployRequest) -> PreflightResult:
        from pathlib import Path

        docs_root = Path("/app/docs")
        exists = docs_root.exists()
        return PreflightResult(
            ok=exists,
            blockers=[] if exists else ["docs_mount_missing:/app/docs"],
            warnings=[],
            details={"docs_root": str(docs_root), "mounted": exists},
        )

    async def start(self, request: DeployRequest) -> DeployStartResult:
        return DeployStartResult(
            started=True,
            status="no_rollout_required",
            detail="docs/ is bind-mounted into aads-server; committed files are served immediately",
            ownership=self.ownership,
        )
