#!/bin/bash
# Start one allowlisted unified component deployment outside the API request.

set -euo pipefail

RUN_ID="${1:-}"
TRIGGER="${2:-manual}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${AADS_DEPLOY_STATE_DIR:-/root/aads/aads-server}"
LOG_DIR="${STATE_DIR}/logs"

if [[ ! "$RUN_ID" =~ ^[0-9]+$ ]]; then
    echo "invalid deploy run id"
    exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "deferred_to_host_drain: docker CLI unavailable (run=${RUN_ID})"
    exit 0
fi

mkdir -p "$LOG_DIR"
LOCKFILE="/tmp/aads-unified-component-${RUN_ID}.lock"
if [[ -s "$LOCKFILE" ]]; then
    holder="$(tr -dc '0-9' < "$LOCKFILE")"
    if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
        echo "unified component worker already running: pid=${holder}, run=${RUN_ID}"
        exit 0
    fi
fi

LOGFILE="${LOG_DIR}/unified-component-${RUN_ID}.log"
nohup python3 "${SCRIPT_DIR}/unified_component_deploy_worker.py" \
    --run-id "$RUN_ID" --trigger "$TRIGGER" --lock-file "$LOCKFILE" \
    >>"$LOGFILE" 2>&1 </dev/null &
worker_pid=$!
printf '%s\n' "$worker_pid" > "$LOCKFILE"
echo "unified component worker started: pid=${worker_pid}, run=${RUN_ID}, log=${LOGFILE}"
