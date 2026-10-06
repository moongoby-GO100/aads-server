#!/usr/bin/env bash
# ── 116 로컬 Pipeline Runner 안전 재시작 (AADS-RUNNER-SYNC-BUSY-DEFER) ────
#
# 왜 필요한가. sync_pipeline_runner_remote.sh 는 원격 3개 호스트(contabo14 /
# cafe24_114 / jinah244)만 본다. 116 로컬 러너(aads-pipeline-runner.service)는
# 그 경로 밖이라 아무 보호 없이 `systemctl restart` 로 재시작돼 왔다.
# 2026-09-16 10:29 에 실제로 그렇게 재시작했고, 그때 AADS 작업이 돌고 있었다면
# 같은 날 GO100 runner-1791da41 이 당한 runner_shutdown_requeued 사고가
# 그대로 재현됐을 것이다.
#
# 이 래퍼는 원격 동기화와 **같은 규칙**(runner_busy_lib.sh)으로 판정한다.
#
# 종료코드: 0=재시작함 / 3=실행중 작업 때문에 미룸 / 2=사용법 오류
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="${AADS_LOCAL_RUNNER_SERVICE:-aads-pipeline-runner.service}"
IGNORE_BUSY="${AADS_RUNNER_SYNC_IGNORE_BUSY:-0}"
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: restart_local_runner.sh [--service NAME] [--ignore-busy] [--dry-run]

실행 중인 작업이 있으면 재시작하지 않고 종료코드 3 으로 끝낸다.
--ignore-busy 는 실제 작업자 부재가 확인된 뒤 DB busy count만 우회한다.
살아 있는 프로세스 또는 조회 불명은 이 옵션으로 우회할 수 없다.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)
            SERVICE="${2:-}"
            [[ -n "$SERVICE" ]] || { echo "ERROR: --service requires a value" >&2; exit 2; }
            shift 2
            ;;
        --ignore-busy)
            IGNORE_BUSY=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

log() {
    printf '[%s] %s\n' "$(TZ=Asia/Seoul date '+%F %T KST')" "$*"
}

# shellcheck source=scripts/runner_busy_lib.sh
source "${SCRIPT_DIR}/runner_busy_lib.sh"

# 러너가 pipeline_jobs.runner_host 에 쓰는 이름과 똑같이 해석한다.
local_runner_host_name() {
    local name=""
    name=$(systemctl show -p Environment --value "$SERVICE" 2>/dev/null \
             | tr ' ' '\n' | sed -n 's/^AADS_RUNNER_HOST_NAME=//p' | head -1) || name=""
    name="${name//[[:space:]]/}"
    if [[ -z "$name" ]]; then
        name=$(hostname -s 2>/dev/null || true)
        name="${name//[[:space:]]/}"
    fi
    printf '%s' "$name"
}

process_state=$(runner_live_process_probe "$SERVICE") || process_state="UNKNOWN"
if should_defer_for_processes "$process_state"; then
    log "재시작 보류 — live process state=${process_state}; DB/ignore-busy cannot override"
    exit 3
fi

maintenance_state=$(runner_maintenance_metadata preflight /run/aads-runner-maintenance "$SERVICE") || maintenance_state=UNKNOWN
if [[ "$maintenance_state" != AWARE ]]; then
    log "재시작 보류 — BOOTSTRAP_REQUIRED: legacy/unknown actor cannot honor maintenance leases"
    exit 3
fi

host_name=$(local_runner_host_name)
busy_count=$(db_active_job_count "$host_name")
service_state=$(systemctl is-active "$SERVICE" 2>/dev/null || true)
service_state="${service_state//[[:space:]]/}"

log "runner=${SERVICE} host=${host_name:-unknown} active_jobs=${busy_count:-unknown} service=${service_state:-unknown}"

if should_defer_for_busy "$busy_count" "$IGNORE_BUSY" "$service_state"; then
    log "재시작 보류 — 실행 중 작업이 있거나 판별 불가. 작업이 끝난 뒤 다시 실행하십시오(--ignore-busy 로 강제 가능)."
    exit 3
fi

if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: would systemctl restart ${SERVICE}"
    exit 0
fi

restart_under_maintenance() {
    local restart_plan
    restart_plan=$(runner_maintenance_metadata restart-plan /run/aads-runner-maintenance "$SERVICE") || return 1
    systemctl restart "$SERVICE" || return 1
    systemctl is-active "$SERVICE" || return 1
    [[ "$(runner_maintenance_metadata restart-verify /run/aads-runner-maintenance "$SERVICE" "$restart_plan")" == AWARE ]]
}
runner_maintenance_transaction "$SERVICE" 30 /run/aads-runner-maintenance restart_under_maintenance || exit "$?"
sleep 3
log "재시작 완료 — state=$(systemctl is-active "$SERVICE" 2>/dev/null || true) pid=$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || true)"
