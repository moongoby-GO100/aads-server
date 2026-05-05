#!/bin/bash
LOG="/tmp/contabo-full-sync.log"
CONTABO="root@5.104.86.116"
exec > $LOG 2>&1

echo "[$(date '+%H:%M:%S')] Step 2: SCP dump to Contabo..."
scp -o StrictHostKeyChecking=no -o ConnectTimeout=10 /tmp/aads_sync.dump $CONTABO:/tmp/aads_sync.dump
echo "[$(date '+%H:%M:%S')] SCP done"

echo "[$(date '+%H:%M:%S')] Step 3: Restore DB..."
ssh -o StrictHostKeyChecking=no $CONTABO "docker cp /tmp/aads_sync.dump aads-postgres:/tmp/aads_sync.dump"
ssh -o StrictHostKeyChecking=no $CONTABO "docker exec aads-postgres pg_restore -U aads -d aads --clean --if-exists --no-owner -j 2 /tmp/aads_sync.dump" 2>&1 | tail -20
echo "[$(date '+%H:%M:%S')] Restore done"

echo "[$(date '+%H:%M:%S')] Step 4: Rebuild aads-server..."
ssh -o StrictHostKeyChecking=no $CONTABO "cd /root/aads/aads-server && docker compose -f docker-compose.prod.yml build aads-server" 2>&1 | tail -5
echo "[$(date '+%H:%M:%S')] Build done"

echo "[$(date '+%H:%M:%S')] Step 5: Restart server..."
ssh -o StrictHostKeyChecking=no $CONTABO "cd /root/aads/aads-server && docker compose -f docker-compose.prod.yml up -d --no-deps aads-server" 2>&1
echo "[$(date '+%H:%M:%S')] Restart done"

echo "[$(date '+%H:%M:%S')] Step 6: Rebuild dashboard..."
ssh -o StrictHostKeyChecking=no $CONTABO "cd /root/aads/aads-dashboard && docker compose build aads-dashboard" 2>&1 | tail -5
ssh -o StrictHostKeyChecking=no $CONTABO "cd /root/aads/aads-dashboard && docker compose up -d --no-deps aads-dashboard" 2>&1
echo "[$(date '+%H:%M:%S')] Dashboard done"

sleep 15
echo "[$(date '+%H:%M:%S')] Step 7: Health check..."
ssh -o StrictHostKeyChecking=no $CONTABO "curl -sf http://localhost:8100/api/v1/health"
echo ""
echo "[$(date '+%H:%M:%S')] === ALL DONE ==="
