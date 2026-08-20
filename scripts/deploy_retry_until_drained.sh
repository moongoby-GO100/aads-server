#!/usr/bin/env bash
# Retry bluegreen deploy until the standby (target) slot has no active streams.
# Never force-deploys: it only waits for the guard condition to clear.
set -uo pipefail

LOG="/tmp/deploy_retry_$(date +%Y%m%d_%H%M).log"
MAX_MINUTES="${MAX_MINUTES:-45}"
INTERVAL="${INTERVAL:-90}"
DEADLINE=$(( $(date +%s) + MAX_MINUTES * 60 ))

echo "[retry] start $(date '+%F %T %Z') max=${MAX_MINUTES}m interval=${INTERVAL}s" | tee -a "$LOG"

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    OUT="$(bash /root/aads/aads-server/deploy.sh bluegreen 2>&1)"
    echo "$OUT" >> "$LOG"

    if echo "$OUT" | grep -q "활성 스트림"; then
        echo "[retry] $(date '+%T') target slot busy — wait ${INTERVAL}s" | tee -a "$LOG"
        sleep "$INTERVAL"
        continue
    fi

    if echo "$OUT" | grep -qE "배포 완료|deploy completed|✅ Blue-Green"; then
        echo "[retry] $(date '+%T') deploy SUCCESS" | tee -a "$LOG"
        exit 0
    fi

    echo "[retry] $(date '+%T') deploy stopped for another reason — see $LOG" | tee -a "$LOG"
    exit 1
done

echo "[retry] deadline reached without deploying" | tee -a "$LOG"
exit 2
