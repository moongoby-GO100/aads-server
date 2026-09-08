"""Adapters for projects whose rollout is executed by their own runner/host.

These adapters keep the *central ledger* authoritative (deploy_runs /
deploy_components rows are always created) while being explicit that the actual
rollout command is owned by the project side. That distinction is what the PRD
calls ``project_runner`` ownership: AADS records and displays the deploy, the
project host performs it.
"""

from __future__ import annotations

from app.services.deploy_adapters.base import (
    OWNER_PROJECT_RUNNER,
    BaseDeployAdapter,
    DeployRequest,
    DeployStartResult,
)


class ProjectRunnerAdapter(BaseDeployAdapter):
    """Generic ledger-only adapter for remote-owned deployments."""

    ownership = OWNER_PROJECT_RUNNER
    remote_command: str = ""
    remote_host: str = ""

    def describe(self) -> dict:
        data = super().describe()
        data["remote_host"] = self.remote_host
        data["remote_command"] = self.remote_command
        return data

    async def start(self, request: DeployRequest) -> DeployStartResult:
        return DeployStartResult(
            started=False,
            status="external_project_queue_only",
            detail=(
                f"{self.project}/{self.component} rollout is owned by {self.remote_host or 'the project host'}: "
                f"run `{self.remote_command or 'the project deploy script'}`. "
                "The central ledger row is created and tracked here."
            ),
            ownership=self.ownership,
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


class SfAdapter(ProjectRunnerAdapter):
    project = "SF"
    component = "api"
    deploy_type = "api_bluegreen"
    remote_host = "cafe24_114 (114.207.244.86)"
    remote_command = "bash /data/shortflow/deploy.sh"
    supports_rollback = False


class NasAdapter(ProjectRunnerAdapter):
    project = "NAS"
    component = "worker"
    deploy_type = "worker_restart"
    remote_host = "cafe24_114 (114.207.244.86)"
    remote_command = "project-owned worker restart"
    supports_rollback = False
