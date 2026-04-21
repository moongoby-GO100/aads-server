#!/bin/bash
# Chat-Direct Hook용 대시보드 재빌드 트리거 (비동기 즉시 반환)
# - 이미 build-dashboard.sh가 돌고 있으면 스킵 (중복 빌드 방지)
# - nohup으로 백그라운드 기동 → 호출자는 즉시 exit 0
# - 로그: /tmp/dash-build.log

set -u
LOG="/tmp/dash-build.log"
TS() { date '+%Y-%m-%d %H:%M:%S KST'; }

if pgrep -f "aads-server/scripts/build-dashboard.sh" > /dev/null; then
    echo "[$(TS)] [trigger] SKIP — build-dashboard.sh already running" | tee -a "$LOG"
    exit 0
fi

# 마지막 빌드 완료로부터 60초 이내면 스킵 (파일 mtime 기준)
NOW_EPOCH=$(date +%s)
LAST_EPOCH=0
if [ -f "$LOG" ]; then
    LAST_EPOCH=$(stat -c %Y "$LOG" 2>/dev/null || echo 0)
fi
DIFF=$(( NOW_EPOCH - LAST_EPOCH ))
if [ "$DIFF" -lt 60 ] && [ "$LAST_EPOCH" -gt 0 ]; then
    echo "[$(TS)] [trigger] SKIP — cooldown (${DIFF}s < 60s)" | tee -a "$LOG"
    exit 0
fi

echo "[$(TS)] [trigger] START — dashboard rebuild dispatched" | tee -a "$LOG"
nohup bash /root/aads/aads-server/scripts/build-dashboard.sh >> "$LOG" 2>&1 &
echo "[$(TS)] [trigger] dispatched pid=$!" | tee -a "$LOG"
exit 0
