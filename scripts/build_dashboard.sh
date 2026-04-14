#!/bin/bash
# 대시보드 빌드+배포 스크립트
set -e
LOG=/tmp/dashboard-build.log
echo "[$(date)] 빌드 시작" > "$LOG"
cd /root/aads/aads-dashboard
docker compose build aads-dashboard >> "$LOG" 2>&1
echo "[$(date)] 빌드 완료, 배포 시작" >> "$LOG"
docker compose up -d aads-dashboard >> "$LOG" 2>&1
echo "[$(date)] 배포 완료" >> "$LOG"
# 헬스체크 대기
sleep 15
if curl -s -o /dev/null -w "%{http_code}" http://localhost:3100 | grep -q "200"; then
  echo "[$(date)] 헬스체크 OK" >> "$LOG"
else
  echo "[$(date)] 헬스체크 WARN: 응답 확인 필요" >> "$LOG"
fi
echo "DONE" >> "$LOG"
