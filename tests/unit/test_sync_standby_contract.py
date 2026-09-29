"""Contracts for deferred standby convergence after a partial release."""

from pathlib import Path


ROOT = Path(__file__).parents[2]
DEPLOY = (ROOT / "deploy.sh").read_text(encoding="utf-8")
SYNC = (ROOT / "scripts/sync-standby.sh").read_text(encoding="utf-8")


def test_partial_release_schedules_host_side_standby_retry():
    assert "schedule_standby_sync_retry()" in DEPLOY
    assert 'schedule_standby_sync_retry "$DEPLOY_RUN_ID"' in DEPLOY
    assert "systemd-run" in DEPLOY
    assert "systemd-run --no-block" in DEPLOY
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


def _certify_body() -> str:
    return SYNC.split("certify_deferred_run() {", 1)[1].split("\n}\n", 1)[0]


def test_certification_records_provenance_only_after_the_update_changed_a_row():
    body = _certify_body()
    update_at = body.index("UPDATE deploy_runs")
    returning_at = body.index("RETURNING deploy_run_id")
    call_at = body.index("record_provenance_after_certify")
    assert update_at < returning_at < call_at
    assert '== "$DEPLOY_RUN_ID"' in body
    assert '"$DRY_RUN" == "true" ]] || record_provenance_after_certify' in body


def test_provenance_call_is_non_fatal_and_uses_certified_release():
    fn = SYNC.split("record_provenance_after_certify() {", 1)[1].split("\n}\n", 1)[0]
    for needle in (
        '--repo "$REPO_ROOT"',
        '--deploy-run-id "$DEPLOY_RUN_ID"',
        "--project AADS",
        "--component api",
        '--release-ref "$release_sha"',
        "|| log",
    ):
        assert needle in fn, needle
    assert "record-release-provenance.sh" in fn
    assert "INSERT INTO deploy_release_provenance" not in SYNC

