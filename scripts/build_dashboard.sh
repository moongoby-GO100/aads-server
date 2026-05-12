#!/bin/bash
# 대시보드 blue-green 배포 스크립트
set -euo pipefail
LOG=/tmp/dashboard-build.log
echo "[$(date)] blue-green 배포 시작" > "$LOG"
/root/aads/aads-dashboard/deploy.sh >> "$LOG" 2>&1
echo "[$(date)] blue-green 배포 완료" >> "$LOG"
