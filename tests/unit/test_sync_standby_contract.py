"""Contracts for deferred standby convergence after a partial release."""

from pathlib import Path


ROOT = Path(__file__).parents[2]
DEPLOY = (ROOT / "deploy.sh").read_text(encoding="utf-8")
SYNC = (ROOT / "scripts/sync-standby.sh").read_text(encoding="utf-8")


def test_partial_release_schedules_host_side_standby_retry():
    assert "schedule_standby_sync_retry()" in DEPLOY
    assert 'schedule_standby_sync_retry "$DEPLOY_RUN_ID"' in DEPLOY
    assert "systemd-run" in DEPLOY
    assert "--deploy-run-id" in DEPLOY


def test_sync_never_rebuilds_or_moves_traffic():
    assert "--no-build --no-deps --force-recreate" in SYNC
    assert "nginx -s reload" not in SYNC
    assert "aads-upstream.conf" not in SYNC
    assert "docker build" not in SYNC
    assert "executing_count" in SYNC


def test_sync_promotes_only_matching_partial_run():
    assert "status='success_partial'" in SYNC
    assert "release_sha='${release_sql}'" in SYNC
    assert "image_digest='${digest_sql}'" in SYNC
    assert "standby_digest='${digest_sql}'" in SYNC
