from datetime import datetime, timezone
import asyncio
import importlib.util
import re
from pathlib import Path
import sys
from types import SimpleNamespace

sys.modules.setdefault(
    "structlog",
    SimpleNamespace(get_logger=lambda *args, **kwargs: SimpleNamespace(warning=lambda *args, **kwargs: None)),
)
_MODULE_PATH = Path(__file__).parents[2] / "app/services/deploy_observability.py"
_SPEC = importlib.util.spec_from_file_location("deploy_observability_under_test", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
get_deploy_status = _MODULE.get_deploy_status
DEPLOY_SCRIPT = Path(__file__).parents[2] / "deploy.sh"
DOCKERFILE = Path(__file__).parents[2] / "Dockerfile"
OPS_API = Path(__file__).parents[2] / "app/api/ops.py"
STREAM_CLASSIFIER = Path(__file__).parents[2] / "scripts/classify_deploy_streams.py"


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


def test_stalled_active_deploy_requires_reconciliation_not_live_deploy_blocker():
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
    blockers = result["next_deploy_readiness"]["blockers"]
    assert active["stalled"] is True
    assert active["started_at"] == old
    assert active["completed_at"] is None
    assert active["effective_status"] == "stalled"
    assert active["signal"] == "deploy_phase_stalled"
    assert active["reconcile_action"] == "deploy_sh_reconcile_before_next_release"
    assert "deployment_reconciliation_required" in blockers
    assert "deployment_in_progress" not in blockers


def test_recent_completed_deployments_include_display_times():
    now = datetime.now(timezone.utc)
    conn = FakeConnection(
        {"deploy_runs", "deploy_history", "pipeline_jobs"},
        {
            "FROM deploy_runs dr": [{
                "id": 9,
                "project": "AADS",
                "release_sha": "deadbeef",
                "status": "success",
                "phase": "completed",
                "requested_at": now,
                "created_at": now,
                "phase_started_at": now,
                "phase_completed_at": now,
                "updated_at": now,
                "image_digest": "sha256:a",
                "standby_digest": "sha256:a",
                "request_payload": {"title": "release title", "changed_files": ["deploy.sh"]},
                "bg_sync_status": "synced",
            }],
            "FROM deploy_recent_durations": [],
            "recent_terminal_deploy_history": [],
            "FROM deploy_phase_events": [],
            "legacy_started_without_terminal_match": [],
            "ROUND(AVG(duration_s)": [],
            "FROM pipeline_jobs": [],
        },
    )

    result = asyncio.run(get_deploy_status(conn))

    completed = result["recent_completed_deployments"][0]
    assert completed["started_at"] == now
    assert completed["completed_at"] == now
    assert completed["release_title"] == "release title"


def test_recent_deployments_include_failed_run_id_and_status():
    now = datetime.now(timezone.utc)
    conn = FakeConnection(
        {"deploy_runs", "deploy_history", "pipeline_jobs"},
        {
            "FROM deploy_runs dr": [],
            "recent_terminal_deploy_history": [{
                "id": 196,
                "project": "AADS",
                "release_sha": "deadbeef",
                "status": "failed",
                "phase": "build_candidate_image",
                "requested_at": now,
                "phase_started_at": now,
                "phase_completed_at": now,
                "updated_at": now,
                "request_payload": {},
                "image_digest": None,
                "standby_digest": None,
                "bg_sync_status": "unknown",
            }],
            "FROM deploy_recent_durations": [],
            "FROM deploy_phase_events": [],
            "legacy_started_without_terminal_match": [],
            "ROUND(AVG(duration_s)": [],
            "FROM pipeline_jobs": [],
        },
    )

    result = asyncio.run(get_deploy_status(conn))

    recent = result["recent_deployments"][0]
    assert recent["id"] == 196
    assert recent["status"] == "failed"
    assert recent["completed_at"] == now


def test_project_deployments_include_projects_from_pipeline_history():
    now = datetime.now(timezone.utc)
    conn = FakeConnection(
        {"deploy_runs", "deploy_history", "pipeline_jobs"},
        {
            "FROM deploy_runs dr": [],
            "project_deploy_overview_latest_runs": [{
                "project": "AADS",
                "release_sha": "deadbeef",
                "status": "success",
                "phase": "completed",
                "requested_at": now,
                "created_at": now,
                "phase_started_at": now,
                "phase_completed_at": now,
                "updated_at": now,
                "duration_ms": 120000,
            }],
            "FROM deploy_recent_durations": [],
            "recent_terminal_deploy_history": [],
            "FROM deploy_phase_events": [],
            "legacy_started_without_terminal_match": [],
            "ROUND(AVG(duration_s)": [],
            "project_deploy_overview_latest_pipeline": [{
                "project": "GO100",
                "runner_job_id": "runner-go100",
                "status": "error",
                "phase": "review_failed",
                "release_sha": "feedbee",
                "created_at": now,
                "started_at": now,
                "completed_at": now,
                "deployed_at": None,
                "updated_at": now,
            }],
            "project_deploy_overview_latest_success_pipeline": [],
            "FROM pipeline_jobs": [],
        },
    )

    result = asyncio.run(get_deploy_status(conn))

    by_project = {item["project"]: item for item in result["project_deployments"]}
    assert set(by_project) == {"AADS", "FOOD", "GO100", "KIS", "SF", "NTV2", "NAS"}
    assert by_project["AADS"]["source"] == "deploy_runs"
    assert by_project["GO100"]["source"] == "pipeline_jobs"
    assert by_project["GO100"]["status"] == "error"
    assert by_project["GO100"]["runner_job_id"] == "runner-go100"


def test_component_deployments_include_manifest_metadata():
    now = datetime.now(timezone.utc)
    conn = FakeConnection(
        {"deploy_runs", "deploy_history", "deploy_components"},
        {
            "FROM deploy_runs dr": [],
            "FROM deploy_recent_durations": [],
            "FROM deploy_phase_events": [],
            "legacy_started_without_terminal_match": [],
            "ROUND(AVG(duration_s)": [],
            "component_deploy_overview_latest_components": [{
                "component_id": 3,
                "id": 11,
                "project": "AADS",
                "component": "dashboard",
                "deploy_type": "dashboard_bluegreen",
                "target_env": "production",
                "release_sha": "cafebabe",
                "status": "success",
                "phase": "completed",
                "started_at": now,
                "completed_at": now,
                "updated_at": now,
                "duration_ms": 90000,
                "health_url": "https://aads.newtalk.kr/login",
                "route_url": "https://aads.newtalk.kr",
                "image_digest": "sha256:a",
                "standby_digest": "sha256:a",
                "release_title": "dashboard deploy",
                "release_summary": "dashboard deploy",
                "changed_files": "[\"src/app/chat/ChatArtifactPanel.tsx\"]",
                "changed_file_count": 1,
                "source": "deploy_components",
            }],
        },
    )

    result = asyncio.run(get_deploy_status(conn))

    components = result["component_deployments"]
    assert components[0]["project"] == "AADS"
    assert components[0]["component"] == "dashboard"
    assert components[0]["deploy_type"] == "dashboard_bluegreen"
    assert components[0]["changed_files"] == ["src/app/chat/ChatArtifactPanel.tsx"]


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
    assert "AADS_DEPLOY_DIRTY_OVERRIDE_REASON" in script
    assert "dirty worktree override requires" in script
    assert "last_heartbeat_at=NOW()" in script
    assert "start_deploy_heartbeat" in script
    assert "AADS_DEPLOY_HEARTBEAT_SECONDS:-15" in script
    assert "DOCKER_BUILDKIT=\"${DOCKER_BUILDKIT:-1}\" docker build" in script
    assert "--target \"${AADS_DOCKER_TARGET}\"" in script
    assert "--build-arg \"INSTALL_PLAYWRIGHT=${AADS_INSTALL_PLAYWRIGHT}\"" in script
    assert "org.opencontainers.image.revision=${AADS_RELEASE_SHA}" in script
    assert "deploy_signal_trap TERM" in script
    assert "ensure_deploy_observability_schema" in script
    assert "migrations/150_deploy_observability_v1.sql" in script
    assert "INSERT INTO deploy_components" in script
    assert "UPDATE deploy_components" in script
    assert "active_streams=${TARGET_STREAMS:-unknown}; elapsed=${local_target_elapsed}s" in script
    assert "reconcile_inactive_target_recovery_executions \"$NEW_CONTAINER\"" in script
    assert "scripts/classify_deploy_streams.py" in script
    assert "--mode live-count" in script
    assert "--mode reconcile" in script
    assert "AADS_DEPLOY_STALE_STREAM_APPLY:-false" in script
    assert "AADS_DEPLOY_STALE_HEARTBEAT_TTL_SECONDS:-90" in script
    assert "AADS_DEPLOY_DEFAULT_ESTIMATE_MS:-600000" in script
    assert "FROM deploy_history" in script
    assert "AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT:-1800" in script
    # ee9aeeef(RC9)에서 standby 동기화 상한이 600→300초로 조정됐다.
    # 값이 아니라 "상한이 존재한다"는 계약을 고정한다.
    assert re.search(r"AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-\d+", script)
    assert "AADS_DEPLOY_STANDBY_SYNC_MIN_WAIT:-10" in script
    assert "AADS_DEPLOY_STANDBY_SYNC_POLL_SECONDS:-5" in script
    assert "AADS_DEPLOY_STANDBY_ZERO_SAMPLES:-1" in script
    assert "AADS_DEPLOY_MIN_FREE_GB:-20" in script
    assert "AADS_DEPLOY_MAX_RELEASE_CONTEXT_MB:-1024" in script
    assert "AADS_DEPLOY_MAX_IMAGE_GB:-7" in script
    assert "build disk preflight failed" in script
    assert "release context too large" in script
    assert "release image too large" in script
    assert "--no-build --no-deps --force-recreate" in script
    assert "reconcile_stale_deploy_runs" in script
    assert "stale deploy reconciled before new deploy" in script
    assert "auto_start" in script
    assert "'committed', 'pushed', TRUE" in script
    assert "COALESCE(request_source, 'deploy.sh_lock_busy')" in script
    assert "reconcile_inactive_target_recovery_executions \"$old_container\"" in script
    assert "DEPLOY_PHASE_METADATA_JSON" in script
    assert "queued_for_deploy" in script
    assert "queue_pending_deploy_request" in script
    assert "start_deploy_queue_worker \"lock_busy\"" in script
    assert "claim_latest_queued_deploy_request" in script
    assert "superseded_by_newer_deploy" in script
    assert "active_same_release" in script
    assert "no duplicate queue created" in script


def test_stream_classifier_excludes_hidden_recovery_retry_from_live_count():
    spec = importlib.util.spec_from_file_location("stream_classifier_under_test", STREAM_CLASSIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    result = module.summarize([
        {
            "id": "exec-1",
            "status": "retrying",
            "error_message": "recovery_auto_retry_scheduled",
            "lease_active": True,
            "heartbeat_age_seconds": 1,
            "hidden_placeholder_count": 1,
            "visible_placeholder_count": 0,
            "assistant_content_chars": 0,
        }
    ])

    assert result["live_count"] == 0
    assert result["classes"] == {"stale_recovery_retry": 1}
    assert result["stale_cancel_candidates"] == ["exec-1"]


def test_deploy_script_keeps_five_minute_monitoring_default():
    script = DEPLOY_SCRIPT.read_text()

    assert 'MONITOR_SECONDS="${AADS_DEPLOY_P0P1_MONITOR_SECONDS:-300}"' in script
    assert "docker logs \"$ACTIVE_CONTAINER\" --since \"$MONITOR_SINCE\"" in script
    assert "record_deploy \"success\"" in script
    # rindex: record_deploy "success" 는 시그널 트랩(전환 후 인터럽트 처리)에도
    # 나오므로 first occurrence 를 쓰면 순서 계약이 아니라 트랩 위치를 재게 된다.
    # 검증 대상은 "최종 성공 기록이 5분 모니터링 게이트 뒤에 온다"는 것이다.
    assert script.index("deploy_phase_start \"p0p1_monitoring\"") < script.rindex("record_deploy \"success\"")


def test_dockerfile_keeps_runtime_image_bounded():
    dockerfile = DOCKERFILE.read_text()

    assert "syntax=docker/dockerfile" in dockerfile
    assert "type=cache,target=/root/.cache/pip" in dockerfile
    assert "type=cache,target=/var/cache/apt" in dockerfile
    assert "FROM python:3.12-slim AS wheelhouse" in dockerfile
    assert "FROM python:3.12-slim AS runtime" in dockerfile
    assert "rustup.rs" not in dockerfile
    assert "/root/.cargo" not in dockerfile
    assert 'ARG INSTALL_PLAYWRIGHT=false' in dockerfile
    assert "requirements.runtime.lock" in dockerfile
    assert 'if [ "$INSTALL_PLAYWRIGHT" = "true" ]' in dockerfile
    assert dockerfile.count("playwright install chromium --with-deps") == 1


def test_ops_deploy_request_kicks_worker_and_returns_followup_state():
    api = OPS_API.read_text()

    assert "async def _start_aads_deploy_queue_worker" in api
    assert "start_aads_deploy_queue_worker.sh" in api
    assert "worker_start" in api
    assert "ops_api_request" in api
    assert '"/api/v1/ops/deploy/status"' in api
