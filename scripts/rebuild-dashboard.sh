#!/bin/bash
echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] Dashboard rebuild START"
cd /root/aads/aads-dashboard
docker compose build --no-cache aads-dashboard
docker compose up -d aads-dashboard
echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] Dashboard rebuild DONE"
