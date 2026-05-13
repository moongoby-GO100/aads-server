#!/bin/bash
LOG=/tmp/dashboard_build_$(date +%Y%m%d_%H%M%S).log
echo "[$(date)] Dashboard rebuild started" > $LOG
cd /root/aads/aads-dashboard
docker compose build aads-dashboard >> $LOG 2>&1
BUILD_EXIT=$?
echo "[$(date)] Build exit code: $BUILD_EXIT" >> $LOG
if [ $BUILD_EXIT -eq 0 ]; then
  docker compose up -d aads-dashboard >> $LOG 2>&1
  echo "[$(date)] Deploy exit code: $?" >> $LOG
else
  echo "[$(date)] Build failed, skipping deploy" >> $LOG
fi
echo "[$(date)] Done" >> $LOG
