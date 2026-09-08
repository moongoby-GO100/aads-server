"""Canonical execution targets for the unified deployment control plane.

The registry is deliberately data-only and allowlisted.  Neither API payloads
nor release metadata can supply a host, working directory, or shell command.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionTarget:
    project: str
    component: str
    deploy_type: str
    executor: str
    host: str
    port: int
    repo_path: str
    command: tuple[str, ...]
    health_command: tuple[str, ...] = ()
    health_url: str | None = None
    route_url: str | None = None
    supports_rollback: bool = False
    require_clean_repo: bool = True
    timeout_seconds: int = 1800

    @property
    def key(self) -> tuple[str, str]:
        return self.project.upper(), self.component.lower()


TARGETS: tuple[ExecutionTarget, ...] = (
    ExecutionTarget(
        project="GO100",
        component="backend",
        deploy_type="backend_graceful_reload",
        executor="ssh",
        host="5.104.86.14",
        port=22,
        repo_path="/root/kis-autotrade-v4",
        command=("bash", "/root/kis-autotrade-v4/scripts/deploy.sh"),
        health_command=("curl", "-fsS", "--max-time", "15", "http://localhost:8002/health"),
        health_url="http://localhost:8002/health",
        route_url="https://go100.newtalk.kr",
        supports_rollback=True,
    ),
    ExecutionTarget(
        project="GO100",
        component="frontend",
        deploy_type="dashboard_bluegreen",
        executor="ssh",
        host="5.104.86.14",
        port=22,
        repo_path="/root/kis-autotrade-v4",
        command=("bash", "/root/kis-autotrade-v4/scripts/deploy_frontend_blue_green.sh", "--apply"),
        health_command=("curl", "-fsS", "--max-time", "15", "https://go100.newtalk.kr/auth/login"),
        health_url="https://go100.newtalk.kr/auth/login",
        route_url="https://go100.newtalk.kr",
        supports_rollback=True,
    ),
    ExecutionTarget(
        project="KIS",
        component="backend",
        deploy_type="backend_graceful_reload",
        executor="ssh",
        host="5.104.86.14",
        port=22,
        repo_path="/root/kis-autotrade-v4",
        command=("bash", "/root/kis-autotrade-v4/scripts/deploy.sh"),
        health_command=("curl", "-fsS", "--max-time", "15", "http://localhost:8000/health"),
        health_url="http://localhost:8000/health",
        supports_rollback=True,
    ),
    ExecutionTarget(
        project="FOOD",
        component="store-assistant",
        deploy_type="docker_service_replace",
        executor="local",
        host="contabo116",
        port=22,
        repo_path="/root/aads/aads-server",
        command=("bash", "/root/aads/aads-server/scripts/deploy_food_store_assistant.sh"),
        health_command=("curl", "-fsS", "--max-time", "15", "https://fb.newtalk.kr/health/live"),
        health_url="http://127.0.0.1:8110/health/live",
        route_url="https://fb.newtalk.kr/health/live",
        supports_rollback=True,
        timeout_seconds=300,
    ),
    ExecutionTarget(
        project="NTV2",
        component="frontend",
        deploy_type="docker_service_replace",
        executor="ssh",
        host="114.207.244.86",
        port=7916,
        repo_path="/srv/newtalk-v2",
        command=("bash", "/srv/newtalk-v2/deploy.sh", "frontend"),
        health_command=("curl", "-fsS", "--max-time", "15", "http://localhost:3000"),
        health_url="http://localhost:3000",
        route_url="https://newtalk.kr",
        supports_rollback=False,
        timeout_seconds=1800,
    ),
    ExecutionTarget(
        project="NTV2",
        component="app",
        deploy_type="php_optimize_reload",
        executor="ssh",
        host="114.207.244.86",
        port=7916,
        repo_path="/srv/newtalk-v2",
        command=("bash", "/srv/newtalk-v2/deploy.sh", "app"),
        health_command=("curl", "-fsS", "--max-time", "15", "http://localhost:8080"),
        health_url="http://localhost:8080",
        route_url="https://newtalk.kr",
        supports_rollback=False,
        timeout_seconds=600,
    ),
    ExecutionTarget(
        project="SF",
        component="worker",
        deploy_type="worker_restart",
        executor="ssh",
        host="114.207.244.86",
        port=7916,
        repo_path="/data/shortflow",
        command=("bash", "/data/shortflow/deploy.sh", "worker"),
        health_command=("docker", "inspect", "shortflow-worker", "--format", "{{.State.Running}}"),
        supports_rollback=False,
        timeout_seconds=300,
    ),
    ExecutionTarget(
        project="SF",
        component="dashboard",
        deploy_type="worker_restart",
        executor="ssh",
        host="114.207.244.86",
        port=7916,
        repo_path="/data/shortflow",
        command=("bash", "/data/shortflow/deploy.sh", "dashboard"),
        health_command=("curl", "-fsS", "--max-time", "15", "http://localhost:8501/_stcore/health"),
        health_url="http://localhost:8501/_stcore/health",
        supports_rollback=False,
        timeout_seconds=300,
    ),
    ExecutionTarget(
        project="SF",
        component="saas",
        deploy_type="docker_service_replace",
        executor="ssh",
        host="114.207.244.86",
        port=7916,
        repo_path="/data/shortflow",
        command=("bash", "/data/shortflow/deploy.sh", "saas"),
        health_command=("curl", "-fsS", "--max-time", "15", "http://localhost:3001"),
        health_url="http://localhost:3001",
        supports_rollback=False,
        timeout_seconds=1800,
    ),
    ExecutionTarget(
        project="NAS",
        component="backup",
        deploy_type="nas_backup_verify",
        executor="ssh",
        host="114.207.244.86",
        port=7916,
        repo_path="",
        command=("bash", "/root/server114/nas_final_check_and_deploy.sh"),
        supports_rollback=False,
        require_clean_repo=False,
        timeout_seconds=180,
    ),
    ExecutionTarget(
        project="AADS",
        component="db",
        deploy_type="db_migration",
        executor="local",
        host="contabo116",
        port=22,
        repo_path="/root/aads/aads-server",
        command=("bash", "/root/aads/aads-server/scripts/deploy_release_assets.sh", "db"),
        supports_rollback=False,
        require_clean_repo=False,
        timeout_seconds=900,
    ),
    ExecutionTarget(
        project="AADS",
        component="config",
        deploy_type="config_prompt_release",
        executor="local",
        host="contabo116",
        port=22,
        repo_path="/root/aads/aads-server",
        command=("bash", "/root/aads/aads-server/scripts/deploy_release_assets.sh", "config"),
        supports_rollback=False,
        require_clean_repo=False,
        timeout_seconds=900,
    ),
    ExecutionTarget(
        project="AADS",
        component="prompt",
        deploy_type="config_prompt_release",
        executor="local",
        host="contabo116",
        port=22,
        repo_path="/root/aads/aads-server",
        command=("bash", "/root/aads/aads-server/scripts/deploy_release_assets.sh", "prompt"),
        supports_rollback=False,
        require_clean_repo=False,
        timeout_seconds=900,
    ),
)

TARGET_BY_KEY = {target.key: target for target in TARGETS}


def get_execution_target(project: str, component: str) -> ExecutionTarget | None:
    return TARGET_BY_KEY.get(((project or "").upper(), (component or "").lower()))
