"""Same-release certification must watch the live service without mutating it."""
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/deploy_acct_app_cafe24.sh"
SHA = "a" * 40
HARNESS = r'''
set -euo pipefail
source "$SCRIPT"
REPO=/unused
validate_state_paths() { :; }
acquire_lock() { :; }
validate_release() { :; }
cleanup_release_wt() { :; }
discover_state() {
 PREV_CONTAINER=acct-app-candidate-r9
 OLD_PORT=8112
 UP_IP=172.18.0.5
 PREV_IMAGE_ID=sha256:immutable
 CAND_LINES=("$PREV_CONTAINER|true|8112|$PREV_IMAGE_ID|$RELEASE_SHA")
}
snapshot_unrelated() { echo unchanged; }
check_unrelated_unchanged() { return 0; }
image_info() { echo "${IMAGE_TEST_ID:-sha256:immutable}|${IMAGE_TEST_SHA:-$RELEASE_SHA}|${IMAGE_TEST_REV:-$SHA8}"; }
candidate_ok() { echo DIRECT_CHECK; [[ ${FAIL_GATE:-} != direct ]]; }
verify_public() { echo PUBLIC_CHECK; [[ ${FAIL_GATE:-} != public ]]; }
sleep() { echo "SLEEP $1"; }
rdocker() {
 if [[ $1 == logs ]]; then
   [[ ${WATCH_FAIL:-} != logs_unavailable ]] || return 1
   [[ ${WATCH_FAIL:-} != p1 ]] || { echo 'P1 application failure'; return; }
   echo clean
 else echo true; fi
}
# Every mutation entrypoint is a failing sentinel; real remote tools are never called.
ssh_c() { echo UNEXPECTED_REMOTE; return 99; }
ensure_image() { echo MUTATION; return 99; }
run_migrations() { echo MUTATION; return 99; }
start_candidate() { echo MUTATION; return 99; }
cutover() { echo MUTATION; return 99; }
apache_switch() { echo MUTATION; return 99; }
docker_mut() { echo MUTATION; return 99; }
if [[ ${WATCH_FAIL:-} == injected ]]; then
 monitor() { echo MONITOR_CALLED; return 77; }
elif [[ ${WATCH_FAIL:-} == public ]]; then
 verify_public() { [[ $MONITOR_TICKS == 0 ]]; }
fi
main_deploy "$SHA" 77 downloadSignedContractPdf
'''


def run(tmp_path, **env):
    return subprocess.run(
        ["bash", "-c", HARNESS], capture_output=True, text=True,
        env={**os.environ, "SCRIPT": str(SCRIPT), "SHA": SHA,
             "ACCT_APP_RESULT_DIR": str(tmp_path), "MONITOR_SECONDS": "300",
             "MONITOR_INTERVAL": "10", **env}, timeout=10,
    )


def result(proc):
    rows = [line.split(" ", 1)[1] for line in proc.stdout.splitlines()
            if line.startswith("ACCT_APP_RELEASE_RESULT ")]
    return json.loads(rows[-1])


def test_active_release_watches_300_seconds_without_mutation(tmp_path):
    proc = run(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    doc = result(proc)
    assert doc["status"] == "already_active"
    assert doc["new"]["image_id"] == "sha256:immutable"
    assert doc["new"]["image_reused"] is True
    assert doc["monitor"] == {"status": "passed", "seconds": 300, "interval": 10, "ticks": 30}
    assert proc.stdout.count("SLEEP 10") == 30
    assert "MUTATION" not in proc.stdout and "UNEXPECTED_REMOTE" not in proc.stdout


@pytest.mark.parametrize("failure", ["injected", "public", "p1", "logs_unavailable"])
def test_watch_failure_blocks_certification_and_preserves_service(tmp_path, failure):
    proc = run(tmp_path, WATCH_FAIL=failure)
    assert proc.returncode == 10, proc.stdout + proc.stderr
    assert result(proc)["status"] == "failed"
    assert result(proc)["recovered_previous_port"] is False
    if failure == "injected":
        assert "MONITOR_CALLED" in proc.stdout
    else:
        assert result(proc)["monitor"]["status"] == "failed"
    assert "MUTATION" not in proc.stdout and "UNEXPECTED_REMOTE" not in proc.stdout


@pytest.mark.parametrize("env", [
    {"IMAGE_TEST_ID": "sha256:wrong"}, {"IMAGE_TEST_SHA": "b" * 40},
    {"IMAGE_TEST_REV": "wrong"}, {"FAIL_GATE": "direct"}, {"FAIL_GATE": "public"},
])
def test_active_release_identity_and_health_fail_closed(tmp_path, env):
    proc = run(tmp_path, **env)
    assert proc.returncode == 12, proc.stdout + proc.stderr
    assert result(proc)["status"] == "failed"
    assert "SLEEP" not in proc.stdout and "MUTATION" not in proc.stdout


def test_short_active_watch_is_rejected_before_checks(tmp_path):
    proc = run(tmp_path, MONITOR_SECONDS="299")
    assert proc.returncode == 2
    assert "DIRECT_CHECK" not in proc.stdout and "SLEEP" not in proc.stdout
