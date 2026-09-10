import importlib.util
import re
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[2] / "scripts" / "classify_deploy_streams.py"
SPEC = importlib.util.spec_from_file_location("classify_deploy_streams", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_classifier_counts_fresh_execution_as_live_tool_wait():
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "status": "running",
        "heartbeat_age_seconds": 10,
        "lease_active": False,
        "lease_missing": False,
        "heartbeat_missing": False,
        "hidden_placeholder_count": 1,
        "visible_placeholder_count": 0,
        "assistant_content_chars": 0,
        "error_message": "",
    }

    result = MODULE.summarize([row], MODULE.StreamPolicy(heartbeat_ttl_seconds=90))

    assert result["classes"] == {"live_tool_wait": 1}
    assert result["live_count"] == 1
    assert result["stale_cancel_candidates"] == []


def test_classifier_marks_expired_hidden_placeholder_as_cancel_candidate():
    row = {
        "id": "00000000-0000-0000-0000-000000000002",
        "status": "retrying",
        "heartbeat_age_seconds": 600,
        "lease_active": False,
        "lease_missing": False,
        "heartbeat_missing": False,
        "hidden_placeholder_count": 1,
        "visible_placeholder_count": 0,
        "assistant_content_chars": 0,
        "error_message": "recovery_auto_retry_scheduled",
    }

    result = MODULE.summarize([row], MODULE.StreamPolicy(heartbeat_ttl_seconds=90))

    assert result["classes"] == {"stale_recovery_retry": 1}
    assert result["live_count"] == 0
    assert result["stale_cancel_candidates"] == [row["id"]]


def test_classifier_preserves_visible_assistant_content():
    row = {
        "id": "00000000-0000-0000-0000-000000000003",
        "status": "running",
        "heartbeat_age_seconds": 900,
        "lease_active": False,
        "lease_missing": False,
        "heartbeat_missing": False,
        "hidden_placeholder_count": 1,
        "visible_placeholder_count": 0,
        "assistant_content_chars": 42,
        "error_message": "recovery_auto_retry_scheduled",
    }

    result = MODULE.summarize([row], MODULE.StreamPolicy(heartbeat_ttl_seconds=90))

    assert result["classes"] == {"live_user_stream": 1}
    assert result["live_count"] == 1
    assert result["stale_cancel_candidates"] == []


def test_unknown_remains_live_for_fail_closed_deploy_gate():
    row = {
        "id": "00000000-0000-0000-0000-000000000004",
        "status": "running",
        "heartbeat_age_seconds": None,
        "lease_active": False,
        "lease_missing": True,
        "heartbeat_missing": True,
        "hidden_placeholder_count": 0,
        "visible_placeholder_count": 0,
        "assistant_content_chars": 0,
        "error_message": "",
    }

    result = MODULE.summarize([row], MODULE.StreamPolicy(heartbeat_ttl_seconds=90))

    assert result["classes"] == {"unknown": 1}
    assert result["live_count"] == 1
    assert result["stale_cancel_candidates"] == []


def test_deploy_script_uses_classifier_and_keeps_monitoring_contract():
    deploy_script = (Path(__file__).parents[2] / "deploy.sh").read_text()

    assert "scripts/classify_deploy_streams.py" in deploy_script
    assert "--mode live-count" in deploy_script
    assert "--mode reconcile" in deploy_script
    assert "AADS_DEPLOY_STALE_STREAM_APPLY:-false" in deploy_script
    assert "AADS_DEPLOY_STALE_HEARTBEAT_TTL_SECONDS:-90" in deploy_script
    # ee9aeeef(RC9)에서 600→300초. 값이 아니라 상한 존재를 고정한다.
    assert re.search(r"AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-\d+", deploy_script)
    assert "DEPLOY_PHASE_METADATA_JSON" in deploy_script
    assert 'MONITOR_SECONDS="${AADS_DEPLOY_P0P1_MONITOR_SECONDS:-300}"' in deploy_script


def test_release_contract_allows_bounded_standby_sync_timeout():
    verifier = (Path(__file__).parents[2] / "scripts" / "verify-bluegreen-release-contract.sh").read_text()

    assert "AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-[0-9]+" in verifier
    assert "standby sync must have a bounded default timeout" in verifier


def test_release_contract_accepts_dashboard_archive_from_approved_sha():
    verifier = (Path(__file__).parents[2] / "scripts" / "verify-bluegreen-release-contract.sh").read_text()

    assert 'git -C "$STATE_DIR" archive --format=tar "$AADS_RELEASE_SHA"' in verifier
    assert "dashboard image must be built from an isolated committed release context" in verifier


def test_post_cutover_signal_cannot_certify_an_incomplete_release():
    deploy_script = (Path(__file__).parents[2] / "deploy.sh").read_text()
    trap_body = deploy_script.split("deploy_signal_trap()", 1)[1].split("deploy_process_alive()", 1)[0]

    assert 'deploy_observe_update "failed" "interrupted_post_switch"' in trap_body
    assert "completed_after_signal_recovery" not in trap_body
    assert "certified live" not in trap_body


def test_deploy_db_exec_preserves_successful_query_output():
    deploy_script = (Path(__file__).parents[2] / "deploy.sh").read_text()
    function_body = deploy_script.split("deploy_db_exec()", 1)[1].split(
        "deploy_db_available()", 1
    )[0]

    assert "printf '%s' \"$_db_out\"" in function_body


def test_standby_sync_failure_cannot_be_certified_as_partial_success():
    deploy_script = (Path(__file__).parents[2] / "deploy.sh").read_text()
    failure_path = deploy_script.split(
        'if ! sync_standby_slot_after_drain "$OLD_CONTAINER"', 1
    )[1].split('HEALTH_URL="http://localhost:${NEW_PORT}', 1)[0]

    assert 'record_deploy "failed" "$MODE"' in failure_path
    assert "exit 1" in failure_path
    assert "success_partial" not in failure_path
