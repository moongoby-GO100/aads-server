from datetime import datetime, timezone
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

sys.modules.setdefault(
    "structlog",
    SimpleNamespace(get_logger=lambda: SimpleNamespace(warning=lambda *args, **kwargs: None)),
)
_MODULE_PATH = Path(__file__).parents[2] / "app/services/deploy_observability.py"
_SPEC = importlib.util.spec_from_file_location("deploy_observability_under_test", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
get_deploy_status = _MODULE.get_deploy_status
DEPLOY_SCRIPT = Path(__file__).parents[2] / "deploy.sh"


class FakeConnection:
    def __init__(self, tables, query_rows):
        self.tables = tables
        self.query_rows = query_rows

    async def fetchval(self, _query, table):
        return table.removeprefix("public.") in self.tables

    async def fetch(self, query, *_args):
        for marker, rows in self.query_rows.items():
            if marker in query:
                return rows
        return []


def test_status_degrades_to_legacy_without_new_migration():
    conn = FakeConnection(
        {"deploy_history", "pipeline_jobs"},
        {
            "legacy_started_without_terminal_match": [
                {"deploy_history_id": 1, "project": "AADS", "signal": "legacy_started_without_terminal_match"}
            ],
            "ROUND(AVG(duration_s)": [
                {"project": "AADS", "sample_count": 2, "avg_duration_ms": 120000, "source": "deploy_history"}
            ],
        },
    )

    result = asyncio.run(get_deploy_status(conn))

    assert result["degraded"] is True
    assert "deploy_observability_migration_not_applied" in result["degraded_reasons"]
    assert result["active_deployments"] == []
    assert result["legacy_stale_candidates"][0]["signal"] == "legacy_started_without_terminal_match"
    assert result["recent_durations_per_project"][0]["avg_duration_ms"] == 120000


def test_go100_zombie_blocks_next_deploy_without_mutation():
    now = datetime.now(timezone.utc)
    conn = FakeConnection(
        {"deploy_runs", "deploy_history", "pipeline_jobs"},
        {
            "FROM deploy_runs dr": [],
            "FROM deploy_recent_durations": [],
            "FROM deploy_phase_events": [],
            "legacy_started_without_terminal_match": [],
            "ROUND(AVG(duration_s)": [],
            "FROM pipeline_jobs": [{
                "runner_job_id": "runner-dead",
                "project": "GO100",
                "status": "running",
                "phase": "code_modify",
                "release_sha": "abc123",
                "runner_pid": 999,
                "error_detail": None,
                "started_at": now,
                "created_at": now,
                "updated_at": now,
                "idle_seconds": 3600,
            }],
        },
    )

    result = asyncio.run(get_deploy_status(conn))

    signal = result["stale_zombie_signals"][0]
    assert signal["signal"] == "zombie_candidate"
    assert signal["reconcile_action"] == "review_only"
    assert signal["requires_ceo_approval"] is True
    assert result["next_deploy_readiness"]["ready"] is False
    assert "runner_reconciliation_required" in result["next_deploy_readiness"]["blockers"]


def test_stalled_active_deploy_is_visible_blocker():
    now = datetime.now(timezone.utc)
    old = now.replace(year=now.year - 1)
    conn = FakeConnection(
        {"deploy_runs", "deploy_history", "pipeline_jobs"},
        {
            "FROM deploy_runs dr": [{
                "id": 7,
                "project": "AADS",
                "release_sha": "deadbeef",
                "status": "syncing_standby",
                "phase": "standby_same_digest_sync",
                "phase_started_at": old,
                "last_heartbeat_at": old,
                "updated_at": old,
                "image_digest": "sha256:a",
                "standby_digest": "sha256:b",
                "bg_sync_status": "mismatch",
            }],
            "FROM deploy_recent_durations": [],
            "FROM deploy_phase_events": [],
            "legacy_started_without_terminal_match": [],
            "ROUND(AVG(duration_s)": [],
            "FROM pipeline_jobs": [],
        },
    )

    result = asyncio.run(get_deploy_status(conn))

    active = result["active_deployments"][0]
    assert active["stalled"] is True
    assert active["signal"] == "deploy_phase_stalled"
    assert "deployment_phase_stalled" in result["next_deploy_readiness"]["blockers"]


def test_deploy_script_records_phase_timeline_and_dirty_exclusions():
    script = DEPLOY_SCRIPT.read_text()

    assert "deploy_phase_start \"build_candidate_image\"" in script
    assert "deploy_phase_start \"candidate_health\"" in script
    assert "deploy_phase_start \"nginx_cutover\" \"verifying\"" in script
    assert "deploy_phase_start \"standby_same_digest_sync\" \"syncing_standby\"" in script
    assert "deploy_phase_start \"p0p1_monitoring\" \"verifying\"" in script
    assert "INSERT INTO deploy_phase_events" in script
    assert "UPDATE deploy_runs" in script
    assert "release image excludes uncommitted worktree changes" in script
    assert "enforce_release_worktree_gate" in script
    assert "dirty worktree blocks release" in script
    assert "AADS_DEPLOY_ALLOW_DIRTY_ARCHIVE" in script
    assert "last_heartbeat_at=NOW()" in script
    assert "deploy_signal_trap TERM" in script
    assert "ensure_deploy_observability_schema" in script
    assert "migrations/150_deploy_observability_v1.sql" in script
    assert "active_streams=${TARGET_STREAMS:-unknown}; elapsed=${local_target_elapsed}s" in script
    assert "reconcile_inactive_target_recovery_executions \"$NEW_CONTAINER\"" in script
    assert "COALESCE(te.error_message, '') = 'recovery_auto_retry_scheduled'" in script
    assert "AND COALESCE(m.is_hidden, false) = true" in script
    assert "AADS_DEPLOY_DEFAULT_ESTIMATE_MS:-600000" in script
    assert "FROM deploy_history" in script
    assert "AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT:-180" in script
    assert "AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-300" in script
    assert "AADS_DEPLOY_STANDBY_SYNC_MIN_WAIT:-10" in script
    assert "AADS_DEPLOY_STANDBY_SYNC_POLL_SECONDS:-5" in script
    assert "AADS_DEPLOY_STANDBY_ZERO_SAMPLES:-1" in script
    assert "--force-recreate --no-build --no-deps" in script
    assert "reconcile_stale_deploy_runs" in script
    assert "stale deploy reconciled before new deploy" in script
    assert "reconcile_inactive_target_recovery_executions \"$old_container\"" in script


def test_deploy_script_keeps_five_minute_monitoring_default():
    script = DEPLOY_SCRIPT.read_text()

    assert 'MONITOR_SECONDS="${AADS_DEPLOY_P0P1_MONITOR_SECONDS:-300}"' in script
    assert "docker logs \"$ACTIVE_CONTAINER\" --since \"$MONITOR_SINCE\"" in script
    assert "record_deploy \"success\"" in script
    assert script.index("deploy_phase_start \"p0p1_monitoring\"") < script.index("record_deploy \"success\"")
