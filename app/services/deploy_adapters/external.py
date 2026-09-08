"""Adapters for projects whose rollout is executed by their own runner/host.

These adapters keep the central ledger authoritative and hand execution to the
audited host worker.  The HTTP request never opens SSH itself; the host drain
claims the queued row and invokes only a canonical target from ``targets.py``.
"""

from __future__ import annotations

import asyncio

from app.services.deploy_adapters.base import (
    OWNER_CENTRAL_WORKER,
    BaseDeployAdapter,
    DeployRequest,
    DeployStartResult,
    PreflightResult,
)
from app.services.deploy_adapters.targets import get_execution_target


class ProjectRunnerAdapter(BaseDeployAdapter):
    """Generic adapter executed by the allowlisted host worker."""

    ownership = OWNER_CENTRAL_WORKER
    remote_command: str = ""
    remote_host: str = ""
    launcher_candidates = (
        "/root/aads/aads-server/scripts/start_unified_component_deploy_worker.sh",
        "/app/scripts/start_unified_component_deploy_worker.sh",
    )

    def describe(self) -> dict:
        data = super().describe()
        data["remote_host"] = self.remote_host
        data["remote_command"] = self.remote_command
        return data

    async def preflight(self, request: DeployRequest) -> PreflightResult:
        target = get_execution_target(request.project, request.component)
        blockers: list[str] = []
        if target is None:
            blockers.append("execution_target_not_registered")
        if self.resolve_launcher_path() is None:
            blockers.append("unified_component_worker_unavailable")
        if len(request.release_sha.strip()) < 7:
            blockers.append("release_sha_too_short")
        return PreflightResult(
            ok=not blockers,
            blockers=blockers,
            warnings=["host_preflight_pending"],
            details={
                "execution_target": target.key if target else None,
                "host_preflight": "sha_dirty_command_health_checked_by_host_worker",
            },
        )

    async def start(self, request: DeployRequest) -> DeployStartResult:
        if get_execution_target(request.project, request.component) is None:
            return DeployStartResult(
                False, "execution_target_not_registered",
                detail=f"no allowlisted target for {request.project}/{request.component}",
                ownership=self.ownership,
            )
        launcher = self.resolve_launcher_path()
        if launcher is None:
            return DeployStartResult(False, "launcher_unavailable", ownership=self.ownership)
        if request.deploy_run_id is None:
            return DeployStartResult(False, "deploy_run_id_required", ownership=self.ownership)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", launcher, str(request.deploy_run_id), request.request_source or "ops_api",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.start_timeout_seconds)
        except asyncio.TimeoutError:
            return DeployStartResult(
                True, "start_timeout_assumed_background",
                detail="host worker launcher did not return before the API deadline",
                ownership=self.ownership,
            )
        except Exception as exc:
            return DeployStartResult(False, "start_failed", detail=str(exc), ownership=self.ownership)
        detail = ((stdout or b"") + (stderr or b"")).decode("utf-8", errors="replace").strip()
        lowered = detail.lower()
        if "deferred_to_host_drain" in lowered:
            return DeployStartResult(True, "deferred_to_host_drain", detail=detail, ownership=self.ownership)
        started = proc.returncode == 0 and ("worker started" in lowered or "already running" in lowered)
        return DeployStartResult(
            started, "started" if started else "start_failed", detail=detail,
            ownership=self.ownership, returncode=proc.returncode,
        )


class Go100BackendAdapter(ProjectRunnerAdapter):
    project = "GO100"
    component = "backend"
    deploy_type = "backend_graceful_reload"
    remote_host = "contabo14 (5.104.86.14)"
    remote_command = "bash /root/kis-autotrade-v4/scripts/deploy.sh"
    health_url = "http://localhost:8002/health"
    route_url = "https://go100.newtalk.kr"
    supports_rollback = True


class Go100FrontendAdapter(ProjectRunnerAdapter):
    project = "GO100"
    component = "frontend"
    deploy_type = "dashboard_bluegreen"
    remote_host = "contabo14 (5.104.86.14)"
    remote_command = "bash /root/kis-autotrade-v4/scripts/deploy_frontend_blue_green.sh --apply"
    health_url = "https://go100.newtalk.kr/auth/login"
    route_url = "https://go100.newtalk.kr"
    supports_rollback = True


class KisBackendAdapter(ProjectRunnerAdapter):
    project = "KIS"
    component = "backend"
    deploy_type = "backend_graceful_reload"
    remote_host = "contabo14 (5.104.86.14)"
    remote_command = "bash /root/kis-autotrade-v4/scripts/deploy.sh"
    supports_rollback = True


class Ntv2FrontendAdapter(ProjectRunnerAdapter):
    project = "NTV2"
    component = "frontend"
    deploy_type = "dashboard_bluegreen"
    remote_host = "cafe24_114 (114.207.244.86)"
    remote_command = "bash /srv/newtalk-v2/deploy.sh frontend"
    supports_rollback = True


class Ntv2AppAdapter(ProjectRunnerAdapter):
    project = "NTV2"
    component = "app"
    deploy_type = "php_optimize_reload"
    remote_host = "cafe24_114 (114.207.244.86)"
    remote_command = "bash /srv/newtalk-v2/deploy.sh app"
    supports_rollback = False


class SfWorkerAdapter(ProjectRunnerAdapter):
    project = "SF"
    component = "worker"
    deploy_type = "worker_restart"
    remote_host = "cafe24_114 (114.207.244.86)"
    remote_command = "bash /data/shortflow/deploy.sh worker"
    supports_rollback = False


class SfDashboardAdapter(ProjectRunnerAdapter):
    project = "SF"
    component = "dashboard"
    deploy_type = "worker_restart"
    remote_host = "cafe24_114 (114.207.244.86)"
    remote_command = "bash /data/shortflow/deploy.sh dashboard"


class SfSaasAdapter(ProjectRunnerAdapter):
    project = "SF"
    component = "saas"
    deploy_type = "docker_service_replace"
    remote_host = "cafe24_114 (114.207.244.86)"
    remote_command = "bash /data/shortflow/deploy.sh saas"


class NasAdapter(ProjectRunnerAdapter):
    project = "NAS"
    component = "backup"
    deploy_type = "nas_backup_verify"
    remote_host = "cafe24_114 (114.207.244.86)"
    remote_command = "bash /root/server114/nas_final_check_and_deploy.sh"
    supports_rollback = False


class FoodStoreAssistantAdapter(ProjectRunnerAdapter):
    project = "FOOD"
    component = "store-assistant"
    deploy_type = "docker_service_replace"
    remote_host = "contabo116 (local host)"
    remote_command = "bash /root/aads/aads-server/scripts/deploy_food_store_assistant.sh <release_sha>"
    health_url = "https://fb.newtalk.kr/health/live"
    route_url = "https://fb.newtalk.kr"
    supports_rollback = True


class AadsDatabaseAdapter(ProjectRunnerAdapter):
    project = "AADS"
    component = "db"
    deploy_type = "db_migration"
    remote_host = "contabo116 (local host)"
    remote_command = "bash /root/aads/aads-server/scripts/deploy_release_assets.sh db <release_sha>"


class AadsConfigAdapter(ProjectRunnerAdapter):
    project = "AADS"
    component = "config"
    deploy_type = "config_prompt_release"
    remote_host = "contabo116 (local host)"
    remote_command = "bash /root/aads/aads-server/scripts/deploy_release_assets.sh config <release_sha>"


class AadsPromptAdapter(ProjectRunnerAdapter):
    project = "AADS"
    component = "prompt"
    deploy_type = "config_prompt_release"
    remote_host = "contabo116 (local host)"
    remote_command = "bash /root/aads/aads-server/scripts/deploy_release_assets.sh prompt <release_sha>"


# Compatibility import for callers that used the original single SF adapter.
SfAdapter = SfWorkerAdapter
