"""Unit tests for the unified deploy adapter registry and manual control plane."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

sys.modules.setdefault(
    "structlog",
    SimpleNamespace(
        get_logger=lambda *args, **kwargs: SimpleNamespace(
            warning=lambda *a, **k: None,
            info=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )
    ),
)

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

# Keep this focused unit test independent from optional runtime dependencies
# imported by app.services.__init__ (asyncpg, redis, etc.).  The adapter modules
# are loaded from the real package path below.
services_package = ModuleType("app.services")
services_package.__path__ = [str(ROOT / "app/services")]
sys.modules.setdefault("app.services", services_package)

OPS_API = ROOT / "app/api/ops.py"
DRAIN_SCRIPT = ROOT / "scripts/aads_deploy_drain.sh"
DASHBOARD_WORKER = ROOT / "scripts/start_aads_dashboard_deploy_worker.sh"
DASHBOARD_BODY = ROOT / "scripts/_aads_dashboard_deploy_run.sh"
API_WORKER = ROOT / "scripts/start_aads_deploy_queue_worker.sh"
UNIFIED_LAUNCHER = ROOT / "scripts/start_unified_component_deploy_worker.sh"
UNIFIED_WORKER = ROOT / "scripts/unified_component_deploy_worker.py"
FOOD_DEPLOY = ROOT / "scripts/deploy_food_store_assistant.sh"
ASSET_DEPLOY = ROOT / "scripts/deploy_release_assets.sh"


def _load(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


registry = _load("deploy_adapters_registry_under_test", "app/services/deploy_adapters/registry.py")
control = _load("deploy_control_under_test", "app/services/deploy_control.py")


class FakeConn:
    """Minimal asyncpg-like connection recording executed statements."""

    def __init__(self, row=None, rows=None, fetchval=None):
        self.row = row
        self.rows = rows or {}
        self._fetchval = fetchval
        self.executed = []

    async def fetchrow(self, _query, *_args):
        return self.row

    async def fetch(self, query, *_args):
        for marker, rows in self.rows.items():
            if marker in query:
                return rows
        return []

    async def fetchval(self, *_args, **_kwargs):
        return self._fetchval

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *_exc):
                return False

        return _Tx()


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
def test_registry_covers_aads_and_project_owned_targets():
    coverage = registry.registry_coverage()
    targets = set(coverage["targets"])
    assert {"AADS/api", "AADS/dashboard", "AADS/docs"}.issubset(targets)
    assert {"GO100/backend", "GO100/frontend"}.issubset(targets)
    assert {
        "FOOD/store-assistant", "NTV2/frontend", "NTV2/app",
        "SF/worker", "SF/dashboard", "SF/saas", "NAS/backup",
        "AADS/db", "AADS/config", "AADS/prompt",
    }.issubset(targets)
    assert coverage["central_worker"] >= 16
    assert coverage["project_runner"] == 0


def test_dashboard_adapter_is_central_worker_with_ledger_launcher():
    adapter = registry.resolve_adapter("AADS", "dashboard")
    described = adapter.describe()
    assert described["ownership"] == "central_worker"
    assert described["deploy_type"] == "dashboard_bluegreen"
    assert any("start_aads_dashboard_deploy_worker.sh" in c for c in adapter.launcher_candidates)


def test_component_alias_resolves_to_registered_adapter():
    assert registry.resolve_adapter("GO100", "web").component == "frontend"
    assert registry.is_registered("AADS", "server") is True


def test_unknown_component_falls_back_without_faking_start():
    adapter = registry.resolve_adapter("AADS", "totally-unknown")
    result = asyncio.run(
        adapter.start(
            SimpleNamespace(
                project="AADS",
                component="totally-unknown",
                deploy_type="unregistered",
                release_sha="abc1234",
                target_env="production",
                deploy_run_id=1,
                requested_by="test",
                request_source="test",
                metadata={},
            )
        )
    )
    assert result.started is False
    assert result.status == "execution_target_not_registered"


def test_remote_adapter_is_owned_by_central_host_drain():
    adapter = registry.resolve_adapter("GO100", "frontend")
    assert adapter.ownership == "central_worker"
    assert any("start_unified_component_deploy_worker.sh" in c for c in adapter.launcher_candidates)
    assert "deploy_frontend_blue_green.sh" in adapter.describe()["remote_command"]


def test_execution_target_order_matches_approved_rollout_sequence():
    targets = sys.modules["app.services.deploy_adapters.targets"].TARGETS
    keys = [target.key for target in targets]
    assert keys.index(("FOOD", "store-assistant")) < keys.index(("NTV2", "frontend"))
    assert keys.index(("NTV2", "frontend")) < keys.index(("SF", "worker"))
    assert keys.index(("SF", "worker")) < keys.index(("NAS", "backup"))
    assert keys.index(("NAS", "backup")) < keys.index(("AADS", "db"))


def test_all_execution_targets_are_allowlisted_without_payload_shell_fields():
    for target in sys.modules["app.services.deploy_adapters.targets"].TARGETS:
        assert target.executor in {"local", "ssh"}
        assert target.command
        assert all("{" not in token and "}" not in token or token == "{{.State.Running}}" for token in target.command)
        assert ";" not in " ".join(target.command)


# --------------------------------------------------------------------------
# manual control
# --------------------------------------------------------------------------
def test_cancel_rejects_in_flight_rollout():
    conn = FakeConn(row={"id": 10, "status": "running", "project": "AADS", "component": "api"})
    result = asyncio.run(control.cancel_deploy_run(conn, 10, actor="ceo"))
    assert result["ok"] is False
    assert result["status"] == "active_rollout_not_cancellable"
    assert conn.executed == []


def test_cancel_marks_queued_run_cancelled():
    conn = FakeConn(
        row={"id": 11, "status": "queued", "project": "AADS", "component": "dashboard", "release_sha": "abc"}
    )
    result = asyncio.run(control.cancel_deploy_run(conn, 11, actor="ceo", reason="superseded"))
    assert result["ok"] is True
    assert result["status"] == "cancelled"
    joined = " ".join(q for q, _ in conn.executed)
    assert "UPDATE deploy_runs" in joined
    assert "deploy_phase_events" in joined


def test_retry_rejects_non_terminal_run():
    conn = FakeConn(row={"id": 12, "status": "queued", "project": "AADS", "component": "api"})
    result = asyncio.run(control.retry_deploy_run(conn, 12, actor="cto"))
    assert result["ok"] is False
    assert result["status"] == "not_retryable"


def test_reconcile_dry_run_never_mutates():
    stale = [
        {
            "id": 20,
            "project": "AADS",
            "component": "api",
            "release_sha": "abc",
            "status": "running",
            "phase": "build",
            "deploy_pid": 999999,
            "heartbeat_age_seconds": 4200,
        }
    ]
    conn = FakeConn(
        rows={"status = ANY($1::text[])": stale, "status = 'queued'": [], "deploy_locks": []},
        fetchval=None,
    )
    result = asyncio.run(control.reconcile_deploy_state(conn, dry_run=True))
    assert result["dry_run"] is True
    assert result["would_apply"]["fail_stale_runs"] == [20]
    assert conn.executed == []
    assert result["findings"]["pid_check"] == "unavailable_from_api_container"


# --------------------------------------------------------------------------
# workers / API wiring
# --------------------------------------------------------------------------
def test_api_worker_reports_deferred_instead_of_fake_empty_queue():
    worker = API_WORKER.read_text()
    assert "deferred_to_host_drain" in worker
    assert "command -v docker" in worker


def test_dashboard_worker_syncs_central_ledger():
    launcher = DASHBOARD_WORKER.read_text()
    body = DASHBOARD_BODY.read_text()
    assert "deferred_to_host_drain" in launcher
    assert "_aads_dashboard_deploy_run.sh" in launcher
    for table in ("deploy_runs", "deploy_components", "deploy_phase_events"):
        assert table in body
    assert "duration_ms" in body
    assert "aads-dashboard/deploy.sh" in body or "deploy.sh" in body


def test_drain_script_dispatches_api_and_dashboard():
    drain = DRAIN_SCRIPT.read_text()
    assert "start_aads_deploy_queue_worker.sh" in drain
    assert "start_aads_dashboard_deploy_worker.sh" in drain
    assert "start_unified_component_deploy_worker.sh" in drain
    assert "queued_for_deploy" in drain
    for target in ("FOOD/store-assistant", "NTV2/frontend", "SF/worker", "NAS/backup", "AADS/prompt"):
        assert target in drain


def test_unified_worker_enforces_sha_dirty_lease_and_health_contracts():
    launcher = UNIFIED_LAUNCHER.read_text()
    worker = UNIFIED_WORKER.read_text()
    assert "deferred_to_host_drain" in launcher
    assert "deploy_locks" in worker
    assert "release SHA" in worker
    assert "status" in worker and "--porcelain" in worker
    assert "post-deploy health failed" in worker
    assert "get_execution_target" in worker


def test_food_deploy_reuses_release_image_and_only_replaces_food_service():
    script = FOOD_DEPLOY.read_text()
    assert 'IMAGE="aads-server:${RELEASE_SHA}"' in script
    assert "docker compose" in script
    assert "--no-deps --no-build yeoljeong-finance" in script
    assert "fb.newtalk.kr/health/live" in script
    assert "docker compose down" not in script


def test_release_asset_worker_is_commit_scoped_and_destructive_sql_fails_closed():
    script = ASSET_DEPLOY.read_text()
    assert 'diff --name-only "$base_sha" "$RELEASE_SHA"' in script
    assert "DROP|TRUNCATE" in script
    assert "ON_ERROR_STOP=1" in script
    assert "compiled_prompt_provenance" in script


def test_ops_api_uses_adapter_registry_and_manual_controls():
    api = OPS_API.read_text()
    assert "from app.services.deploy_adapters import DeployRequest, resolve_adapter" in api
    assert '"/ops/deploy/adapters"' in api
    assert '"/ops/deploy/reconcile"' in api
    assert '"/ops/deploy/{run_id}/cancel"' in api
    assert '"/ops/deploy/{run_id}/retry"' in api
    assert '"/ops/deploy/{run_id}/approve"' in api
    assert '"/ops/deploy/{run_id}/logs"' in api
    assert "preflight" in api


def test_asyncpg_concat_parameters_are_explicitly_typed():
    observability = (ROOT / "app/services/deploy_observability.py").read_text()
    controls = (ROOT / "app/services/deploy_control.py").read_text()
    assert "$3::text" in observability
    assert "CONCAT_WS('; ', NULLIF(error_summary, ''), $2)" not in controls
