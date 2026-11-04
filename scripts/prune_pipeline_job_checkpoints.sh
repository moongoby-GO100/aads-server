#!/usr/bin/env bash
# pipeline_job_checkpoints 정리 (migrations/20261104_pipeline_job_checkpoints.sql)
#   1) 종료(done/error/cancelled/rejected/rejected_done) 작업의 체크포인트 삭제
#   2) 보존 기간(기본 7일) 지난 체크포인트 삭제
# 종료 작업 삭제만 필요하면 MODE=terminal, 보존 기간 삭제만이면 MODE=age (M2/M3 에서 호출).
#
# cron 후보(설치는 별도 승인): 17 * * * * /root/aads/aads-server/scripts/prune_pipeline_job_checkpoints.sh
#   기존 chat_outbox_prune.sh / prune_claude_sessions.sh 와 같은 체계에 붙인다.
set -uo pipefail

RETENTION="${CHECKPOINT_RETENTION:-7 days}"
MODE="${MODE:-all}"
CONTAINER="${AADS_PG_CONTAINER:-aads-postgres}"
TIMEOUT_SEC="${CHECKPOINT_PRUNE_TIMEOUT:-60}"

case "${MODE}" in
    all|terminal|age) ;;
    *)
        echo "$(date '+%F %T%z') error reason=invalid_mode value='${MODE}'"
        echo "usage: MODE={all|terminal|age} $0   (all=기본, terminal=종료 작업만, age=보존 기간만)"
        exit 2
        ;;
esac

# 보존 기간은 SQL 에 들어가므로 "숫자 단위" 형식만 허용한다.
if ! [[ "${RETENTION}" =~ ^[0-9]+\ (minutes|hours|days)$ ]]; then
    echo "$(date '+%F %T%z') error reason=invalid_retention value='${RETENTION}'"
    exit 2
fi

# Docker 가 응답하지 않아도 TIMEOUT_SEC 안에 끝나도록 모든 docker 호출에 timeout 을 건다.
running=$(timeout -k 2 "${TIMEOUT_SEC}" docker ps --format '{{.Names}}' 2>/dev/null)
rc=$?
if [ "${rc}" -eq 124 ] || [ "${rc}" -eq 137 ]; then
    echo "$(date '+%F %T%z') error reason=docker_ps_timeout timeout=${TIMEOUT_SEC}s"
    exit 1
fi
if ! printf '%s\n' "${running}" | grep -qx "${CONTAINER}"; then
    echo "$(date '+%F %T%z') skip reason=postgres_container_not_running container=${CONTAINER}"
    exit 0
fi

run_sql() {
    timeout -k 2 "${TIMEOUT_SEC}" docker exec "${CONTAINER}" psql -U aads -d aads -At -v ON_ERROR_STOP=1 -c "$1" 2>&1
}

terminal=0
aged=0
if [ "${MODE}" = "all" ] || [ "${MODE}" = "terminal" ]; then
    terminal=$(run_sql "SELECT public.aads_pipeline_job_checkpoints_purge_terminal()") || {
        echo "$(date '+%F %T%z') error step=terminal detail=${terminal}"; exit 1; }
fi
if [ "${MODE}" = "all" ] || [ "${MODE}" = "age" ]; then
    aged=$(run_sql "SELECT public.aads_pipeline_job_checkpoints_prune(INTERVAL '${RETENTION}')") || {
        echo "$(date '+%F %T%z') error step=age detail=${aged}"; exit 1; }
fi
echo "$(date '+%F %T%z') retention='${RETENTION}' mode=${MODE} deleted_terminal=${terminal} deleted_aged=${aged}"
