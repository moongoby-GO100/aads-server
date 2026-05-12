#!/bin/bash
# AADS-188 대시보드 재배포 래퍼. 직접 compose 교체 대신 blue-green 스크립트 사용.
set -euo pipefail
LOG="/tmp/dashboard_aads188.log"
echo "[$(date '+%H:%M:%S')] blue-green 배포 시작" > "$LOG"
/root/aads/aads-dashboard/deploy.sh >> "$LOG" 2>&1
echo "[$(date '+%H:%M:%S')] blue-green 배포 완료 (exit=$?)" >> "$LOG"
