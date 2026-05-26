#!/bin/bash
set -e
LOG=/tmp/dashboard_deploy_$(date +%s).log
echo "[$(date)] Starting dashboard build..." > "$LOG"
docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard >> "$LOG" 2>&1
echo "[$(date)] Build done. Starting deploy..." >> "$LOG"
bash /root/aads/aads-dashboard/deploy.sh bluegreen >> "$LOG" 2>&1
echo "[$(date)] Deploy complete." >> "$LOG"
echo "DEPLOY_OK" >> "$LOG"
