#!/bin/bash
# Unified deploy queue drain (host side).
#
# The API container can register deploy requests in deploy_runs but cannot run
# the rollout (no docker CLI). This drain runs on the host — from the
# aads-deploy-drain.timer systemd unit, or manually — and dispatches each queued
# AADS component to its adapter worker.
#
# Usage: aads_deploy_drain.sh [trigger]

set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

TRIGGER="${1:-systemd_timer}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_WORKER="${SCRIPT_DIR}/start_aads_deploy_queue_worker.sh"
DASHBOARD_WORKER="${SCRIPT_DIR}/start_aads_dashboard_deploy_worker.sh"

if ! command -v docker >/dev/null 2>&1; then
    echo "drain skipped: docker CLI unavailable (host execution required)"
    exit 0
fi

if ! docker inspect aads-postgres --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
    echo "drain skipped: aads-postgres not running"
    exit 0
fi

db_exec() {
    docker exec aads-postgres psql -U aads -d aads -qAtc "$1" 2>/dev/null || true
}

rows="$(
    db_exec "
        SELECT DISTINCT ON (component) id, component, release_sha
          FROM deploy_runs
         WHERE project='AADS'
           AND status='queued'
           AND phase='queued_for_deploy'
           AND COALESCE(auto_start, FALSE) = TRUE
         ORDER BY component, created_at DESC, id DESC;
    "
)"

if [[ -z "${rows//[[:space:]]/}" ]]; then
    echo "deploy queue empty"
    exit 0
fi

dispatched=0
while IFS='|' read -r run_id component release_sha; do
    run_id="$(printf '%s' "${run_id:-}" | tr -d '[:space:]')"
    component="$(printf '%s' "${component:-}" | tr -d '[:space:]')"
    release_sha="$(printf '%s' "${release_sha:-}" | tr -d '[:space:]')"
    [[ "$run_id" =~ ^[0-9]+$ ]] || continue

    case "$component" in
        api)
            echo "[drain] AADS/api run=${run_id} sha=${release_sha}"
            bash "$API_WORKER" bluegreen "drain_${TRIGGER}" || echo "[drain] api worker exit=$?"
            dispatched=$((dispatched + 1))
            ;;
        dashboard)
            echo "[drain] AADS/dashboard run=${run_id} sha=${release_sha}"
            bash "$DASHBOARD_WORKER" "$release_sha" "$run_id" "drain_${TRIGGER}" || echo "[drain] dashboard worker exit=$?"
            dispatched=$((dispatched + 1))
            ;;
        docs)
            echo "[drain] AADS/docs run=${run_id} — bind-mounted, no rollout required; marking success"
            db_exec "
                UPDATE deploy_runs
                   SET status='success', phase='docs_published',
                       phase_completed_at=NOW(), updated_at=NOW(), last_heartbeat_at=NOW()
                 WHERE id=${run_id} AND status='queued';
            " >/dev/null
            db_exec "
                UPDATE deploy_components
                   SET status='success', phase='docs_published',
                       completed_at=NOW(), updated_at=NOW()
                 WHERE deploy_run_id=${run_id};
            " >/dev/null
            dispatched=$((dispatched + 1))
            ;;
        *)
            echo "[drain] skip unsupported component: ${component} (run=${run_id})"
            ;;
    esac
done <<< "$rows"

echo "drain done: dispatched=${dispatched}, trigger=${TRIGGER}"
