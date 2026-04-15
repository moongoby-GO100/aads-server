#!/bin/bash
# 대시보드 백그라운드 빌드/배포
LOG="/tmp/dashboard_deploy.log"
echo "[$(date '+%H:%M:%S')] 대시보드 빌드 시작" > "$LOG"
cd /root/aads/aads-dashboard
bash deploy.sh >> "$LOG" 2>&1
echo "[$(date '+%H:%M:%S')] 완료 (exit=$?)" >> "$LOG"
