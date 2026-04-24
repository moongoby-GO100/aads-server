#!/bin/bash
echo "[$(date)] Dashboard build started" > /tmp/dashboard-build.log
docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard >> /tmp/dashboard-build.log 2>&1
echo "[$(date)] Build exit: $?" >> /tmp/dashboard-build.log
docker compose -f /root/aads/aads-dashboard/docker-compose.yml up -d aads-dashboard >> /tmp/dashboard-build.log 2>&1
echo "[$(date)] Deploy exit: $?" >> /tmp/dashboard-build.log
echo "BUILD_DONE" >> /tmp/dashboard-build.log
