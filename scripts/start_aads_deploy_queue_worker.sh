#!/bin/bash
# Start the AADS deploy queue worker from an isolated release worktree.
#
# This launcher is intentionally short-lived. It claims the latest queued
# release through deploy.sh, but runs deploy.sh from a clean worktree at that
# release SHA so unrelated dirty files in /root/aads/aads-server cannot be
# silently excluded or block a valid queued release.

set -euo pipefail

MODE="${1:-bluegreen}"
TRIGGER="${2:-manual}"
STATE_DIR="${AADS_DEPLOY_STATE_DIR:-/root/aads/aads-server}"
REPO_DIR="${AADS_DEPLOY_REPO_DIR:-/root/aads/aads-server}"
LOCKFILE="/tmp/aads-deploy-queue-worker.lock"
LOG_DIR="${STATE_DIR}/logs"
mkdir -p "$LOG_DIR"

if ! command -v docker >/dev/null 2>&1; then
    # Running inside the API container: the rollout needs docker on the host.
    # Report honestly instead of printing "deploy queue empty" (which used to
    # look like a successful start while nothing was ever deployed).
    echo "deferred_to_host_drain: docker CLI unavailable here; host drain timer will claim the queued release"
    exit 0
fi

db_exec() {
    docker exec aads-postgres psql -U aads -d aads -qAtc "$1" 2>/dev/null || true
}

if [[ -f "$LOCKFILE" ]]; then
    worker_pid="$(cat "$LOCKFILE" 2>/dev/null || echo "")"
    if [[ -n "$worker_pid" ]] && kill -0 "$worker_pid" 2>/dev/null; then
        echo "deploy queue worker already running: pid=${worker_pid}"
        exit 0
    fi
    rm -f "$LOCKFILE" 2>/dev/null || true
fi

latest_sha="$(
    db_exec "
        SELECT release_sha
        FROM deploy_runs
        WHERE project='AADS'
          AND status='queued'
          AND phase='queued_for_deploy'
          AND COALESCE(auto_start, FALSE) = TRUE
        ORDER BY created_at DESC, id DESC
        LIMIT 1;
    " | tail -1 | tr -d '[:space:]'
)"

if [[ -z "${latest_sha:-}" ]]; then
    echo "deploy queue empty"
    exit 0
fi

if ! git -C "$REPO_DIR" cat-file -e "${latest_sha}^{commit}" 2>/dev/null; then
    echo "queued release not found locally: ${latest_sha}"
    exit 1
fi

log_file="${LOG_DIR}/deploy-queue-worker-$(date +%Y%m%d-%H%M%S)-${latest_sha}.log"
worktree="/tmp/aads-deploy-worker-${latest_sha}-$$"

cleanup_failed_worktree() {
    git -C "$REPO_DIR" worktree remove --force --force "$worktree" >/dev/null 2>&1 || true
}

git -C "$REPO_DIR" worktree add --detach "$worktree" "$latest_sha"

export MODE STATE_DIR REPO_DIR LOCKFILE latest_sha worktree
WORKER_BODY='
    set -euo pipefail
    # systemd-run expands $$ while constructing ExecStart, which previously
    # wrote the literal "$" to the lock file. BASHPID is resolved by the
    # worker shell itself and remains a numeric, kill-checkable owner PID.
    echo "$BASHPID" > "$LOCKFILE"
    cleanup() {
        git -C "$REPO_DIR" worktree remove --force "$worktree" >/dev/null 2>&1 || true
        rm -f "$LOCKFILE" 2>/dev/null || true
    }
    trap cleanup EXIT

    echo "[$(date --iso-8601=seconds)] deploy queue worker start sha=${latest_sha}"
    env \
        AADS_DEPLOY_QUEUE_WORKER=true \
        AADS_DEPLOY_DETACHED=1 \
        AADS_DEPLOY_SOURCE_DIR="$worktree" \
        AADS_DEPLOY_STATE_DIR="$STATE_DIR" \
        AADS_RELEASE_SHA="$latest_sha" \
        bash "$worktree/deploy.sh" "$MODE"
    echo "[$(date --iso-8601=seconds)] deploy queue worker done sha=${latest_sha}"
'

if [[ -d /run/systemd/system ]] && command -v systemd-run >/dev/null 2>&1; then
    # setsid/nohup do not escape a systemd oneshot's cgroup. The drain service
    # exits immediately after dispatch and kills remaining children by default.
    # Give the release its own service and pass only its required environment.
    unit="aads-api-release-${latest_sha:0:12}-$$"
    if ! systemd-run --quiet --collect --unit="$unit" \
        --property=Type=exec \
        --property="StandardOutput=append:${log_file}" \
        --property="StandardError=append:${log_file}" \
        --setenv="MODE=$MODE" --setenv="STATE_DIR=$STATE_DIR" \
        --setenv="REPO_DIR=$REPO_DIR" --setenv="LOCKFILE=$LOCKFILE" \
        --setenv="latest_sha=$latest_sha" --setenv="worktree=$worktree" \
        /bin/bash -c "$WORKER_BODY"; then
        cleanup_failed_worktree
        echo "deploy queue worker service start failed: ${unit}" >&2
        exit 1
    fi
elif command -v setsid >/dev/null 2>&1; then
    setsid -f bash -c "$WORKER_BODY" >"$log_file" 2>&1 < /dev/null
else
    nohup bash -c "$WORKER_BODY" >"$log_file" 2>&1 < /dev/null &
fi

sleep 0.2
worker_pid="$(cat "$LOCKFILE" 2>/dev/null || echo unknown)"
echo "deploy queue worker started: pid=${worker_pid}, sha=${latest_sha}, trigger=${TRIGGER}, log=${log_file}"
