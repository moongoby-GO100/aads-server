#!/usr/bin/env bash
# 디스크 임계치 감시 — 넘으면 즉시 정리한다.
#
# disk_cleanup_v2.sh 는 하루 1회(04:00)만 돈다. 2026-09-12 에는 04:00 정리가
# 정상 동작해 79% → 78% 로 내렸는데, 그 뒤 배포 7번이 릴리스 이미지를 4.24GB씩
# 쌓아 21시에 89% 가 됐다. 다음 정리는 다음 날 04:00 이라 그 사이를 막을 장치가
# 없었고, 결국 배포 #340 이 빌드 직전에 공간 부족으로 막혔다.
#
# 알림 규칙(disk_usage_percent > 80)은 이미 있지만 알림은 정리를 부르지 않는다.
# 이 스크립트가 그 간격을 메운다. 정리 로직은 검증된 v2 를 그대로 쓴다.
set -uo pipefail

THRESHOLD="${AADS_DISK_GUARD_THRESHOLD:-82}"
CLEANUP="/root/aads/aads-server/scripts/disk_cleanup_v2.sh"
STAMP="/tmp/.aads-disk-guard-last"
# 연속 실행으로 정리가 겹치지 않도록 최소 간격을 둔다.
MIN_INTERVAL="${AADS_DISK_GUARD_MIN_INTERVAL:-1800}"

usage_pct() { df -P / | awk 'NR==2 {gsub(/%/,"",$5); print $5}'; }

USED="$(usage_pct)"
[[ "$USED" =~ ^[0-9]+$ ]] || exit 0

if [ "$USED" -lt "$THRESHOLD" ]; then
    exit 0
fi

now=$(date +%s)
if [ -f "$STAMP" ]; then
    last=$(cat "$STAMP" 2>/dev/null || echo 0)
    if [[ "$last" =~ ^[0-9]+$ ]] && [ $((now - last)) -lt "$MIN_INTERVAL" ]; then
        echo "$(date '+%F %T') disk=${USED}% — 최근 정리 후 $((now - last))s, 건너뜀"
        exit 0
    fi
fi
# 배포 중에는 정리하지 않는다.
#
# 빌드가 갓 만든 이미지는 태그가 붙기 전 잠시 dangling 으로 보인다. 그때
# `docker image prune --filter dangling=true` 가 돌면 릴리스 이미지를 지운다.
# 2026-09-13 배포 #393 이 이렇게 깨졌다 — 빌드는 273초에 성공(`naming ... done`)
# 했는데 22:40:15 에 이 스크립트가 정리를 돌렸고, 직후 릴리스 이미지 조회가
# 실패해 배포 전체가 failed 로 끝났다.
#
# 디스크가 아무리 차도 진행 중인 배포를 깨는 것보다는 낫다. 다음 주기에 다시
# 온다. 락은 flock 비차단으로만 본다 — 여기서 기다리면 크론이 쌓인다.
DEPLOY_LOCK="${AADS_DEPLOY_LOCKFILE:-/tmp/aads-deploy.lock}"
if [ -e "$DEPLOY_LOCK" ] && ! flock -n "$DEPLOY_LOCK" true 2>/dev/null; then
    echo "$(date '+%F %T') disk=${USED}% — 배포 진행 중(락 점유), 정리 건너뜀"
    exit 0
fi
# 실행 파일이 docker 인 프로세스만 빌드로 본다.
#
# pgrep -f "docker build" 는 **그 문자열을 담은 아무 셸 명령이나** 잡는다.
# 조사하느라 `ps ... | grep "docker build"` 를 치기만 해도 빌드가 도는 것으로
# 오인한다. 2026-09-14 실측: 하루 동안 정리 0회 / 건너뜀 38회, 그 사이 디스크가
# 85% → 89% 로 올랐다. 정작 실제 빌드는 하루 중 일부였다.
#
# 가드가 가장 필요한 날(배포가 잦은 날)에 한 번도 안 도는 구조였다.
_build_running=$(ps -eo comm=,args= 2>/dev/null | awk '$1 == "docker" && /[ ]build[ ]/ {c++} END{print c+0}')
# 인자 시작을 고정한다. 셸 래퍼(`/bin/bash -c ...`)는 명령 문자열 안에 같은
# 단어를 담고 있어 부분 일치로는 구분되지 않는다 — 실제 배포는
# `bash deploy.sh bluegreen` 처럼 두 번째 인자가 스크립트 이름이다.
_deploy_running=$(ps -eo comm=,args= 2>/dev/null \
    | awk '$1 == "bash" && $3 ~ /(^|\/)deploy\.sh$/ && $4 ~ /^(bluegreen|queue-worker)$/ {c++} END{print c+0}')
if [ "${_build_running:-0}" -gt 0 ] || [ "${_deploy_running:-0}" -gt 0 ]; then
    echo "$(date '+%F %T') disk=${USED}% — 배포/빌드 프로세스 감지(build=${_build_running} deploy=${_deploy_running}), 정리 건너뜀"
    exit 0
fi

printf '%s' "$now" > "$STAMP"

echo "$(date '+%F %T') disk=${USED}% >= ${THRESHOLD}% — 즉시 정리 시작"
if [ -x "$CLEANUP" ]; then
    "$CLEANUP" >/dev/null 2>&1 || true
else
    echo "  cleanup 스크립트 없음: $CLEANUP"
    docker image prune -f --filter "dangling=true" >/dev/null 2>&1 || true
fi
AFTER="$(usage_pct)"
echo "$(date '+%F %T') 정리 완료: ${USED}% → ${AFTER}%"

# 정리하고도 임계치를 못 내리면 사람이 개입해야 한다.
if [[ "$AFTER" =~ ^[0-9]+$ ]] && [ "$AFTER" -ge "$THRESHOLD" ]; then
    echo "$(date '+%F %T') ⚠️ 정리 후에도 ${AFTER}% — 수동 확인 필요"
    ALERT="/root/aads/aads-server/scripts/send_disk_alert.sh"
    [ -x "$ALERT" ] && "$ALERT" "디스크 ${AFTER}% — 자동 정리로 회수 실패" >/dev/null 2>&1 || true
fi
exit 0
