#!/bin/bash
# One-shot background dashboard deploy
# Writes progress to /tmp/dashboard_deploy.log
set -euo pipefail
LOG=/tmp/dashboard_deploy.log
exec > "$LOG" 2>&1
echo "[$(date)] START: dashboard build+deploy"
cd /root/aads/aads-dashboard
echo "[$(date)] Building image..."
docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard
echo "[$(date)] Build complete. Deploying..."
docker compose -f /root/aads/aads-dashboard/docker-compose.yml up -d aads-dashboard
echo "[$(date)] Waiting for health..."
sleep 10
if docker ps --filter "name=aads-dashboard" --filter "health=healthy" --format '{{.Names}}' | grep -q aads-dashboard; then
    echo "[$(date)] DONE: dashboard healthy"
else
    echo "[$(date)] WARNING: dashboard not yet healthy, check manually"
fi
echo "[$(date)] FINISHED"
