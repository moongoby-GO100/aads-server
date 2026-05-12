#!/bin/sh
# 대시보드 blue-green 배포 1회성 스크립트 (crontab에서 호출)
LOG=/tmp/dashboard_build.log
echo "[$(date)] === blue-green 배포 시작 ===" > $LOG
/root/aads/aads-dashboard/deploy.sh >> $LOG 2>&1
echo "[$(date)] === 완료 ===" >> $LOG
# crontab 자체 제거
crontab -l | grep -v "build_dashboard_once" | crontab -
