#!/bin/bash
# 대시보드 Docker 이미지 재빌드 + 재시작
set -e
cd /root/aads/aads-dashboard
echo "[1/3] Building dashboard image..."
docker compose build --no-cache 2>&1 | tail -5
echo "[2/3] Restarting dashboard..."
docker compose up -d 2>&1
echo "[3/3] Waiting for health..."
sleep 10
docker ps --filter name=aads-dashboard --format "{{.Status}}"
echo "[DONE] Dashboard rebuild complete"
