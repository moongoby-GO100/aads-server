#!/bin/sh
# 대시보드 빌드+배포 1회성 스크립트 (crontab에서 호출)
LOG=/tmp/dashboard_build.log
echo "[$(date)] === 빌드 시작 ===" > $LOG
docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard >> $LOG 2>&1
BUILD_EXIT=$?
echo "[$(date)] 빌드 종료 (exit=$BUILD_EXIT)" >> $LOG
if [ $BUILD_EXIT -eq 0 ]; then
  docker compose -f /root/aads/aads-dashboard/docker-compose.yml up -d aads-dashboard >> $LOG 2>&1
  echo "[$(date)] 배포 완료" >> $LOG
  sleep 5
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" https://aads.newtalk.kr/braming)
  echo "[$(date)] HTTP 상태: $STATUS" >> $LOG
else
  echo "[$(date)] 빌드 실패 — 배포 건너뜀" >> $LOG
fi
echo "[$(date)] === 완료 ===" >> $LOG
# crontab 자체 제거
crontab -l | grep -v "build_dashboard_once" | crontab -
