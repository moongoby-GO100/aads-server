"""Release terminal-state and bounded-drain contracts for deploy.sh."""

from pathlib import Path


DEPLOY = (Path(__file__).parents[2] / "deploy.sh").read_text(encoding="utf-8")


def test_inactive_target_drain_is_bounded_to_three_minutes_by_default():
    assert 'AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT:-180}' in DEPLOY
    assert 'AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT:-1800}' not in DEPLOY
    assert 'TARGET_DRAIN_STARTED_EPOCH="$(date +%s)"' in DEPLOY
    assert "local_target_elapsed=$(($(date +%s) - TARGET_DRAIN_STARTED_EPOCH))" in DEPLOY


def test_pre_cutover_wait_does_not_serialize_busy_active_traffic_by_default():
    assert 'AADS_DEPLOY_PRE_CUTOVER_DRAIN_MAX_WAIT:-0}' in DEPLOY
    assert "pre-cutover wait disabled" in DEPLOY
    assert 'set_deploy_stream_phase_metadata "$ACTIVE_CONTAINER"' in DEPLOY


def test_busy_standby_defers_immediately_to_post_monitor_retry_by_default():
    assert 'AADS_DEPLOY_STANDBY_SYNC_MIN_WAIT:-0}' in DEPLOY
    assert 'AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-0}' in DEPLOY
    assert "별도 sync-standby worker" in DEPLOY
    sync = DEPLOY.split("sync_standby_slot_after_drain()", 1)[1].split(
        "# .env에서 텔레그램 변수 로드", 1
    )[0]
    assert sync.index('active="$(stream_count_for_port "$old_port")"') < sync.index(
        'while [[ $elapsed -lt $drain_max ]]'
    )
    assert "return 2" in sync


def test_deferred_standby_sync_remains_success_partial():
    assert 'FINAL_DEPLOY_STATUS="success_partial"' in DEPLOY
    assert 'deploy_observe_update "$FINAL_DEPLOY_STATUS"' in DEPLOY
    assert "status='${FINAL_DEPLOY_STATUS}'" in DEPLOY
    assert "status='success', phase='completed'" not in DEPLOY


def test_uncertified_release_does_not_record_release_provenance():
    provenance = DEPLOY.rsplit(
        'scripts/record-release-provenance.sh" ]]', 1
    )[0].rsplit("\nif ", 1)[1]
    assert '"$FINAL_DEPLOY_STATUS" == "success"' in provenance
