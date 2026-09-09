import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_worker_lock_uses_runtime_bash_pid():
    source = (ROOT / "scripts/start_aads_deploy_queue_worker.sh").read_text()
    worker_body = source.split("WORKER_BODY='", 1)[1].split("'\n\nif [[ -d", 1)[0]

    assert 'echo "$BASHPID" > "$LOCKFILE"' in worker_body
    assert 'echo "$$" > "$LOCKFILE"' not in worker_body


def test_worker_runs_deploy_in_foreground_before_worktree_cleanup():
    source = (ROOT / "scripts/start_aads_deploy_queue_worker.sh").read_text()
    worker_body = source.split("WORKER_BODY='", 1)[1].split("'\n\nif [[ -d", 1)[0]

    assert "AADS_DEPLOY_DETACHED=1" in worker_body
    assert 'bash "$worktree/deploy.sh" "$MODE"' in worker_body


@pytest.mark.parametrize("start_code", [0, 1])
def test_systemd_dispatch_preserves_arguments_and_fails_closed(tmp_path, start_code):
    source = (ROOT / "scripts/start_aads_deploy_queue_worker.sh").read_text()
    branch = source.split('if [[ -d /run/systemd/system ]]', 1)[1]
    branch = branch.split('elif command -v setsid', 1)[0]
    script = '''set -eu
systemd-run() { printf '%s\\n' "$@" > "$ARG_LOG"; return "$START_CODE"; }
cleanup_failed_worktree() { touch "$CLEANUP_LOG"; }
MODE=bluegreen
STATE_DIR='/tmp/state space'
REPO_DIR='/tmp/repo space'
LOCKFILE=/tmp/worker.lock
latest_sha=0123456789abcdef
worktree='/tmp/release space'
log_file='/tmp/log space'
WORKER_BODY='echo "$worktree"; exit 0'
if true''' + branch + 'fi\n'
    env = dict(os.environ, ARG_LOG=str(tmp_path / "args"),
               CLEANUP_LOG=str(tmp_path / "cleaned"), START_CODE=str(start_code))
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == start_code, result.stderr
    args = (tmp_path / "args").read_text().splitlines()
    assert "--property=Type=exec" in args
    assert "--setenv=worktree=/tmp/release space" in args
    assert "--property=StandardOutput=append:/tmp/log space" in args
    assert args[-3:] == ["/bin/bash", "-c", 'echo "$worktree"; exit 0']
    assert (tmp_path / "cleaned").exists() == bool(start_code)


def test_early_failure_observability_without_active_container():
    source = (ROOT / "deploy.sh").read_text()
    functions = []
    for name in ("deploy_observe_update", "deploy_phase_end"):
        functions.append(name + "() {" + source.split(name + "() {", 1)[1].split("\n}\n", 1)[0] + "\n}")
    script = '''set -eu
DEPLOY_CURRENT_PHASE=initializing
DEPLOY_START_EPOCH=$(date +%s)
DEPLOY_PHASE_START_EPOCH=$DEPLOY_START_EPOCH
DEPLOY_RUN_ID=123
deploy_observe_init() { :; }
stop_deploy_heartbeat() { :; }
deploy_estimated_remaining_ms() { echo NULL; }
sql_escape() { printf '%s' "$1"; }
docker() { return 1; }
deploy_db_exec() { printf '%s\\n' "$1" >&2; }
''' + "\n".join(functions) + '''
deploy_phase_end initializing failed 'original failure'
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "INSERT INTO deploy_phase_events" in result.stderr
    assert "UPDATE deploy_runs" in result.stderr
    assert "original failure" in result.stderr
    assert "unbound variable" not in result.stderr
