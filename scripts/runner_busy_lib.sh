#!/usr/bin/env bash
# ── 러너 busy 판정 공용 규칙 (AADS-RUNNER-SYNC-BUSY-DEFER, 2026-09-16) ────
#
# 러너를 재시작하면 실행 중이던 작업은 runner_shutdown_requeued 로 되돌아가
# 처음부터 다시 돈다. 2026-09-16 10:28:51 KST 에 GO100 runner-1791da41(P0)이
# 6분 16초 지점에서 그렇게 버려졌다. 그래서 "지금 재시작해도 되는가" 를
# 판정하는 규칙은 한 곳에만 둔다 — 원격 동기화와 로컬 재시작이 같은 답을 내야 한다.
#
# 사용처:
#   scripts/sync_pipeline_runner_remote.sh  (원격 3개 호스트)
#   scripts/restart_local_runner.sh         (116 로컬 러너)
#
# 이 파일은 source 전용이다. set -e 를 켜지 않는다(호출자 설정을 존중).

PG_CONTAINER="${PG_CONTAINER:-aads-postgres}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"

# 해당 러너 호스트가 붙잡고 있는 작업 수. 조회 실패/이름 미상이면 빈 문자열.
db_active_job_count() {
    local host_name="$1" out=""
    [[ -n "$host_name" ]] || { printf ''; return 0; }
    [[ "$host_name" =~ ^[A-Za-z0-9._-]+$ ]] || { printf ''; return 0; }
    out=$(docker exec -i "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" \
            -q -t -A -P footer=off \
            -c "SELECT count(*) FROM pipeline_jobs WHERE status IN ('claimed','running','deploying') AND runner_host='${host_name}';" \
            </dev/null 2>/dev/null | tr -d '[:space:]') || out=""
    [[ "$out" =~ ^[0-9]+$ ]] || out=""
    printf '%s' "$out"
}

# 0 = 미루기, 1 = 진행.
#
# 인자: <busy_count> [ignore_busy] [service_state]
#
# 판정 순서와 근거:
#  1) ignore_busy=1        → 진행. 러너가 멈춰 작업이 영원히 running 인 경우의 탈출구.
#  2) 서비스가 inactive/failed → 진행. 러너가 죽어 있으면 그 호스트의 in-flight 는
#     좀비이고, 재시작이 곧 복구다. 이게 없으면 죽은 러너의 잔존 job 때문에
#     그 호스트는 5분 타이머마다 영원히 defer 된다.
#  3) 작업 1건 이상        → 미룸.
#  4) 판별 불가(빈 값/비정상) → 미룸(fail-closed). 타이머가 5분 뒤 다시 시도하므로
#     비용은 지연뿐이지만, 잘못 진행하면 P0 작업이 처음부터 다시 돈다.
#
# 시간 기반 stale 판정(updated_at 이 N분 안 움직이면 좀비)은 쓰지 않는다.
# 실측 결과 pipeline_jobs.updated_at 은 status='deploying' 구간에서만 갱신되고
# claude_code_work 중에는 움직이지 않는다 — 정상 작업을 좀비로 오판한다.
should_defer_for_busy() {
    local busy_count="$1" ignore_busy="${2:-0}" service_state="${3:-}"
    [[ "$ignore_busy" == "1" ]] && return 1
    [[ "$service_state" == "inactive" || "$service_state" == "failed" ]] && return 1
    [[ "$busy_count" =~ ^[0-9]+$ ]] || return 0
    [[ "$busy_count" -gt 0 ]] && return 0
    return 1
}
