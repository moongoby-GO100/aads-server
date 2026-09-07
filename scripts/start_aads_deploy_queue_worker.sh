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

export MODE TRIGGER STATE_DIR REPO_DIR LOCKFILE latest_sha worktree
nohup bash -c '
    set -euo pipefail
    cleanup() {
        git -C "$REPO_DIR" worktree remove --force "$worktree" >/dev/null 2>&1 || true
        rm -f "$LOCKFILE" 2>/dev/null || true
    }
    trap cleanup EXIT

    echo "[$(date --iso-8601=seconds)] deploy queue worker start trigger=${TRIGGER} sha=${latest_sha}"
    git -C "$REPO_DIR" worktree add --detach "$worktree" "$latest_sha"
    env \
        AADS_DEPLOY_QUEUE_WORKER=true \
        AADS_DEPLOY_SOURCE_DIR="$worktree" \
        AADS_DEPLOY_STATE_DIR="$STATE_DIR" \
        AADS_RELEASE_SHA="$latest_sha" \
        bash "$worktree/deploy.sh" "$MODE"
    echo "[$(date --iso-8601=seconds)] deploy queue worker done sha=${latest_sha}"
' >"$log_file" 2>&1 < /dev/null &

echo $! > "$LOCKFILE"
echo "deploy queue worker started: pid=$!, sha=${latest_sha}, trigger=${TRIGGER}, log=${log_file}"
