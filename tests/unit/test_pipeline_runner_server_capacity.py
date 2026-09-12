"""Exercise the real shell claim gate with concurrent engine processes."""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/pipeline-runner.sh").read_text()
GATE = SCRIPT.split("claim_queued_job() (", 1)[1].split("\n_claim_queued_job() {", 1)[0]
GATE = "claim_queued_job() (" + GATE


def harness(tmp_path, limit, count="0", fail_db=False):
    state = tmp_path / "count"
    state.write_text(count)
    env = dict(os.environ, MAX_CONCURRENT_SERVER=str(limit),
               RUNNER_CAPACITY_LOCK_FILE=str(tmp_path / "lock"), STATE=str(state))
    # The stub rejects queries without the host's project scope. Mutations
    # simulate the committed claim and deliberately widen the race window.
    stub = """
db_exec() {
    [[ "$1" == *"AND project IN ('SF','NTV2','NAS')"* ]] || return 1
    cat "$STATE"
}
_claim_queued_job() {
    local n
    n=$(cat "$STATE")
    sleep 0.02
    echo $((n + 1)) > "$STATE"
    echo claimed
}
"""
    if fail_db:
        stub += "\ndb_exec() { return 1; }\n"
    return env, stub + GATE + '\nclaim_queued_job "AND project IN (\'SF\',\'NTV2\',\'NAS\')"\n', state


@pytest.mark.parametrize("limit", [10, 20])
def test_engines_share_last_slot_without_oversubscription(tmp_path, limit):
    env, code, state = harness(tmp_path, limit)
    workers = [subprocess.Popen(["bash", "-c", code], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
               for _ in range(limit + 10)]
    results = [worker.communicate(timeout=15) for worker in workers]
    assert all(worker.returncode == 0 for worker in workers)
    assert sum(out == b"claimed\n" for out, err in results) == limit
    assert int(state.read_text()) == limit


@pytest.mark.parametrize("count,fail_db", [("broken", False), ("0", True)])
def test_bad_or_unavailable_count_fails_closed(tmp_path, count, fail_db):
    env, code, state = harness(tmp_path, 10, count, fail_db)
    result = subprocess.run(["bash", "-c", code], env=env, capture_output=True)
    assert result.returncode != 0
    assert state.read_text() == count
    assert not result.stdout


@pytest.mark.parametrize("limit", ["0", "-1", "garbage"])
def test_invalid_limit_cannot_claim(tmp_path, limit):
    env, code, state = harness(tmp_path, limit)
    result = subprocess.run(["bash", "-c", code], env=env, capture_output=True)
    assert result.returncode != 0
    assert state.read_text() == "0"


def test_unconfigured_host_preserves_legacy_claim(tmp_path):
    env, code, state = harness(tmp_path, "")
    result = subprocess.run(["bash", "-c", code], env=env, capture_output=True)
    assert result.returncode == 0
    assert state.read_text().strip() == "1"
