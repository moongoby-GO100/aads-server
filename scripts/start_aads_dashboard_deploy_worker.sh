#!/bin/bash
# AADS Dashboard deploy worker launcher — central ledger + blue/green rollout.
#
# Usage: start_aads_dashboard_deploy_worker.sh [release_sha] [deploy_run_id] [trigger]
#
# Claims the newest queued AADS/dashboard row in deploy_runs and starts the
# detached rollout body (scripts/_aads_dashboard_deploy_run.sh), which writes
# running/success/failed + duration back into deploy_runs, deploy_components
# and deploy_phase_events.
#
# The rollout needs docker on the host. When invoked from inside the API
# container (no docker CLI) it prints "deferred_to_host_drain" so the host
# drain timer claims the queued release instead of silently doing nothing.

set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

RELEASE_SHA_ARG="${1:-}"
DEPLOY_RUN_ID_ARG="${2:-0}"
TRIGGER="${3:-manual}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="${AADS_DASHBOARD_DIR:-/root/aads/aads-dashboard}"
SERVER_DIR="${AADS_DEPLOY_STATE_DIR:-/root/aads/aads-server}"
LOCKFILE="/tmp/aads-dashboard-deploy-worker.lock"
LOG_DIR="${SERVER_DIR}/logs"
HEALTH_EXTERNAL="${DASHBOARD_EXTERNAL_HEALTH_URL:-https://aads.newtalk.kr/login}"
RUN_BODY="${SCRIPT_DIR}/_aads_dashboard_deploy_run.sh"
mkdir -p "$LOG_DIR"

if ! command -v docker >/dev/null 2>&1; then
    echo "deferred_to_host_drain: docker CLI unavailable here; host drain timer will claim the queued dashboard release"
    exit 0
fi

if [[ ! -f "$RUN_BODY" ]]; then
    echo "dashboard deploy worker body missing: ${RUN_BODY}" >&2
    exit 1
fi

db_exec() {
    docker exec aads-postgres psql -U aads -d aads -qAtc "$1" 2>/dev/null || true
}

if [[ -f "$LOCKFILE" ]]; then
    worker_pid="$(cat "$LOCKFILE" 2>/dev/null || echo "")"
    if [[ -n "$worker_pid" ]] && kill -0 "$worker_pid" 2>/dev/null; then
        echo "dashboard deploy worker already running: pid=${worker_pid}"
        exit 0
    fi
    rm -f "$LOCKFILE" 2>/dev/null || true
fi

run_id="${DEPLOY_RUN_ID_ARG}"
[[ "$run_id" =~ ^[0-9]+$ ]] || run_id="0"

if [[ "$run_id" == "0" ]]; then
    run_id="$(
        db_exec "
            SELECT id FROM deploy_runs
             WHERE project='AADS' AND component='dashboard'
               AND status='queued' AND phase='queued_for_deploy'
               AND COALESCE(auto_start, FALSE) = TRUE
             ORDER BY created_at DESC, id DESC
             LIMIT 1;
        " | tail -1 | tr -d '[:space:]'
    )"
fi

if [[ ! "${run_id:-0}" =~ ^[0-9]+$ ]] || [[ "${run_id:-0}" == "0" ]]; then
    echo "dashboard deploy queue empty"
    exit 0
fi

release_sha="${RELEASE_SHA_ARG}"
if [[ -z "$release_sha" ]]; then
    release_sha="$(db_exec "SELECT release_sha FROM deploy_runs WHERE id=${run_id};" | tail -1 | tr -d '[:space:]')"
fi
if [[ -z "$release_sha" ]]; then
    release_sha="$(git -C "$DASHBOARD_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
fi

log_file="${LOG_DIR}/dashboard-deploy-worker-$(date +%Y%m%d-%H%M%S)-${release_sha}.log"

export RUN_ID="$run_id"
export RELEASE_SHA="$release_sha"
export TRIGGER LOCKFILE DASHBOARD_DIR HEALTH_EXTERNAL
export WORKER_LOG_FILE="$log_file"

if command -v setsid >/dev/null 2>&1; then
    setsid -f bash "$RUN_BODY" >"$log_file" 2>&1 < /dev/null
else
    nohup bash "$RUN_BODY" >"$log_file" 2>&1 < /dev/null &
fi

sleep 0.3
worker_pid="$(cat "$LOCKFILE" 2>/dev/null || echo unknown)"
echo "dashboard deploy worker started: pid=${worker_pid}, run=${run_id}, sha=${release_sha}, trigger=${TRIGGER}, log=${log_file}"
