#!/bin/bash
LOG=/tmp/dashboard_build.log
echo "[$(date)] Build started" > $LOG
cd /root/aads/aads-dashboard
docker compose build --no-cache >> $LOG 2>&1
echo "[$(date)] Build exit=$?" >> $LOG
docker compose up -d >> $LOG 2>&1
echo "[$(date)] Up exit=$?" >> $LOG
sleep 10
docker ps --filter name=aads-dashboard --format "{{.Status}}" >> $LOG
echo "[$(date)] DONE" >> $LOG
