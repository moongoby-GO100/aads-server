#!/bin/bash
LOG=/tmp/dashboard_rebuild_nocache.log
echo "[$(date)] Dashboard no-cache rebuild started" > $LOG
cd /root/aads/aads-dashboard
docker compose build --no-cache aads-dashboard >> $LOG 2>&1
BUILD_EXIT=$?
echo "[$(date)] Build exit code: $BUILD_EXIT" >> $LOG
if [ $BUILD_EXIT -eq 0 ]; then
  docker compose up -d --force-recreate aads-dashboard >> $LOG 2>&1
  echo "[$(date)] Deploy exit code: $?" >> $LOG
  docker ps --filter name=aads-dashboard --format '{{.Names}} {{.Status}}' >> $LOG
else
  echo "[$(date)] Build failed, skipping deploy" >> $LOG
fi
echo "[$(date)] Done" >> $LOG
