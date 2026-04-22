#!/bin/bash
# AADS-188 대시보드 재빌드/재배포 (LlmKeyManager 반영)
LOG="/tmp/dashboard_aads188.log"
echo "[$(date '+%H:%M:%S')] 빌드 시작" > "$LOG"
docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard >> "$LOG" 2>&1
echo "[$(date '+%H:%M:%S')] 빌드 완료 (exit=$?)" >> "$LOG"
docker compose -f /root/aads/aads-dashboard/docker-compose.yml up -d aads-dashboard >> "$LOG" 2>&1
echo "[$(date '+%H:%M:%S')] 배포 완료 (exit=$?)" >> "$LOG"
