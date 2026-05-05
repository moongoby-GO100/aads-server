#!/bin/bash
LOG="/tmp/contabo-green.log"
CONTABO="root@5.104.86.116"
exec > $LOG 2>&1

echo "[$(date '+%H:%M:%S')] Building green containers on Contabo..."

# aads-server-green (same image, different port)
echo "[$(date '+%H:%M:%S')] Starting aads-server-green..."
ssh -o StrictHostKeyChecking=no $CONTABO "cd /root/aads/aads-server && docker compose -f docker-compose.prod.yml up -d --no-deps aads-server-green" 2>&1 | tail -5

echo "[$(date '+%H:%M:%S')] Starting aads-dashboard-green..."
ssh -o StrictHostKeyChecking=no $CONTABO "cd /root/aads/aads-server && docker compose -f docker-compose.prod.yml up -d --no-deps aads-dashboard-green" 2>&1 | tail -5

echo "[$(date '+%H:%M:%S')] Waiting for healthy..."
sleep 20

echo "[$(date '+%H:%M:%S')] Status check..."
ssh -o StrictHostKeyChecking=no $CONTABO "docker ps --format '{{.Names}} {{.Status}}' | sort"

echo "[$(date '+%H:%M:%S')] === GREEN DONE ==="
