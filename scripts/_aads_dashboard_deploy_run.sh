#!/bin/bash
# Detached body for the AADS Dashboard deploy worker.
# Invoked only by scripts/start_aads_dashboard_deploy_worker.sh.
#
# Required env: RUN_ID, RELEASE_SHA, TRIGGER, LOCKFILE, DASHBOARD_DIR,
#               HEALTH_EXTERNAL, WORKER_LOG_FILE

set -uo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

RUN_ID="${RUN_ID:?RUN_ID required}"
RELEASE_SHA="${RELEASE_SHA:-unknown}"
TRIGGER="${TRIGGER:-manual}"
LOCKFILE="${LOCKFILE:-/tmp/aads-dashboard-deploy-worker.lock}"
DASHBOARD_DIR="${DASHBOARD_DIR:-/root/aads/aads-dashboard}"
HEALTH_EXTERNAL="${HEALTH_EXTERNAL:-https://aads.newtalk.kr/login}"
WORKER_LOG_FILE="${WORKER_LOG_FILE:-/tmp/aads-dashboard-deploy-worker.log}"

echo "$$" > "$LOCKFILE"
cleanup() { rm -f "$LOCKFILE" 2>/dev/null || true; }
trap cleanup EXIT

db_exec() {
    docker exec aads-postgres psql -U aads -d aads -qAtc "$1" 2>/dev/null || true
}

sql_escape() {
    printf "%s" "${1:-}" | sed "s/'/''/g"
}

started_epoch="$(date +%s)"
log_sql="$(sql_escape "$WORKER_LOG_FILE")"

db_exec "
    UPDATE deploy_runs
       SET status='running', phase='dashboard_bluegreen_rollout',
           phase_started_at=NOW(), updated_at=NOW(), last_heartbeat_at=NOW(),
           deploy_pid=$$
     WHERE id=${RUN_ID};
" >/dev/null

db_exec "
    INSERT INTO deploy_components(deploy_run_id, project, component, deploy_type, release_sha,
                                  status, phase, started_at, log_path, metadata, created_at, updated_at)
    SELECT ${RUN_ID}, 'AADS', 'dashboard', 'dashboard_bluegreen', release_sha,
           'running', 'dashboard_bluegreen_rollout', NOW(), '${log_sql}',
           jsonb_build_object('worker', 'start_aads_dashboard_deploy_worker.sh'), NOW(), NOW()
      FROM deploy_runs WHERE id=${RUN_ID}
       AND NOT EXISTS (SELECT 1 FROM deploy_components
                        WHERE deploy_run_id=${RUN_ID} AND component='dashboard');
" >/dev/null

db_exec "
    UPDATE deploy_components
       SET status='running', phase='dashboard_bluegreen_rollout',
           started_at=COALESCE(started_at, NOW()), updated_at=NOW(), log_path='${log_sql}'
     WHERE deploy_run_id=${RUN_ID} AND component='dashboard';
" >/dev/null

echo "[$(date --iso-8601=seconds)] dashboard deploy worker start run=${RUN_ID} sha=${RELEASE_SHA} trigger=${TRIGGER}"

deploy_rc=0
( cd "$DASHBOARD_DIR" && AADS_RELEASE_SHA="$RELEASE_SHA" bash "${DASHBOARD_DIR}/deploy.sh" ) || deploy_rc=$?

http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$HEALTH_EXTERNAL" 2>/dev/null || echo 000)"
finished_epoch="$(date +%s)"
duration_ms=$(( (finished_epoch - started_epoch) * 1000 ))

if [[ "$deploy_rc" -eq 0 ]] && [[ "$http_code" =~ ^(200|302|307)$ ]]; then
    final_status="success"
    detail="dashboard blue/green rollout ok (health=${http_code}, log=${WORKER_LOG_FILE})"
else
    final_status="failed"
    detail="dashboard rollout failed rc=${deploy_rc} health=${http_code} log=${WORKER_LOG_FILE}"
fi
detail_sql="$(sql_escape "$detail")"

db_exec "
    UPDATE deploy_runs
       SET status='${final_status}',
           phase='dashboard_rollout_${final_status}',
           phase_completed_at=NOW(), updated_at=NOW(), last_heartbeat_at=NOW(),
           error_summary=CASE WHEN '${final_status}'='failed'
                              THEN CONCAT_WS('; ', NULLIF(error_summary, ''), '${detail_sql}')
                              ELSE error_summary END
     WHERE id=${RUN_ID};
" >/dev/null

db_exec "
    UPDATE deploy_components
       SET status='${final_status}',
           phase='dashboard_rollout_${final_status}',
           completed_at=NOW(), updated_at=NOW(), duration_ms=${duration_ms},
           health_url='${HEALTH_EXTERNAL}',
           error_summary=CASE WHEN '${final_status}'='failed' THEN '${detail_sql}' ELSE NULL END
     WHERE deploy_run_id=${RUN_ID} AND component='dashboard';
" >/dev/null

db_exec "
    INSERT INTO deploy_phase_events(deploy_run_id, phase, status, phase_started_at,
                                    phase_completed_at, duration_ms, error_summary, metadata)
    VALUES(${RUN_ID}, 'dashboard_bluegreen_rollout', '${final_status}',
           NOW() - (${duration_ms} || ' milliseconds')::interval, NOW(), ${duration_ms},
           '${detail_sql}',
           jsonb_build_object('worker', 'start_aads_dashboard_deploy_worker.sh',
                              'trigger', '${TRIGGER}',
                              'health_code', '${http_code}'));
" >/dev/null

echo "[$(date --iso-8601=seconds)] dashboard deploy worker done run=${RUN_ID} status=${final_status} rc=${deploy_rc} health=${http_code}"
