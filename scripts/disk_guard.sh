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
