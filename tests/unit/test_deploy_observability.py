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
