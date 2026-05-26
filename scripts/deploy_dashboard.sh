#!/bin/bash
set -e
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Dashboard build start"
cd /root/aads/aads-dashboard
docker compose build aads-dashboard 2>&1 | tail -10
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Build done, deploying..."
docker compose up -d aads-dashboard 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Deploy complete"
sleep 15
wget --spider -q http://127.0.0.1:3100 && echo "HEALTH: OK" || echo "HEALTH: FAIL"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done"
