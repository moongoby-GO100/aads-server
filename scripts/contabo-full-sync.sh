#!/bin/bash
# Contabo 완전 동기화 스크립트 — 서버68에서 실행
set -e
LOG="/tmp/contabo-full-sync.log"
CONTABO="root@5.104.86.116"
echo "[$(date '+%H:%M:%S')] === START ===" > $LOG

# Step 1: DB 덤프
echo "[$(date '+%H:%M:%S')] Step 1: DB dump..." >> $LOG
docker exec aads-postgres pg_dump -U aads -d aads -Fc -f /tmp/aads_sync.dump 2>> $LOG
docker cp aads-postgres:/tmp/aads_sync.dump /tmp/aads_sync.dump >> $LOG 2>&1
echo "[$(date '+%H:%M:%S')] DB dump done ($(du -h /tmp/aads_sync.dump | cut -f1))" >> $LOG

# Step 2: DB 덤프 전송
echo "[$(date '+%H:%M:%S')] Step 2: Transfer dump..." >> $LOG
scp -o StrictHostKeyChecking=no /tmp/aads_sync.dump $CONTABO:/tmp/aads_sync.dump >> $LOG 2>&1
echo "[$(date '+%H:%M:%S')] Transfer done" >> $LOG

# Step 3: Contabo DB 복원
echo "[$(date '+%H:%M:%S')] Step 3: DB restore on Contabo..." >> $LOG
ssh -o StrictHostKeyChecking=no $CONTABO "docker cp /tmp/aads_sync.dump aads-postgres:/tmp/aads_sync.dump" >> $LOG 2>&1
ssh -o StrictHostKeyChecking=no $CONTABO "docker exec aads-postgres pg_restore -U aads -d aads --clean --if-exists --no-owner -j 2 /tmp/aads_sync.dump 2>&1 | tail -5" >> $LOG 2>&1
echo "[$(date '+%H:%M:%S')] DB restore done" >> $LOG

# Step 4: Docker 리빌드 (aads-server)
echo "[$(date '+%H:%M:%S')] Step 4: Docker rebuild on Contabo..." >> $LOG
ssh -o StrictHostKeyChecking=no $CONTABO "cd /root/aads/aads-server && docker compose -f docker-compose.prod.yml build aads-server 2>&1 | tail -3" >> $LOG 2>&1
echo "[$(date '+%H:%M:%S')] Build done" >> $LOG

# Step 5: 컨테이너 재시작
echo "[$(date '+%H:%M:%S')] Step 5: Restart aads-server..." >> $LOG
ssh -o StrictHostKeyChecking=no $CONTABO "cd /root/aads/aads-server && docker compose -f docker-compose.prod.yml up -d --no-deps aads-server 2>&1" >> $LOG 2>&1
echo "[$(date '+%H:%M:%S')] Server restart done" >> $LOG

# Step 6: Dashboard 리빌드
echo "[$(date '+%H:%M:%S')] Step 6: Dashboard rebuild..." >> $LOG
ssh -o StrictHostKeyChecking=no $CONTABO "cd /root/aads/aads-dashboard && docker compose build aads-dashboard 2>&1 | tail -3" >> $LOG 2>&1
ssh -o StrictHostKeyChecking=no $CONTABO "cd /root/aads/aads-dashboard && docker compose up -d --no-deps aads-dashboard 2>&1" >> $LOG 2>&1
echo "[$(date '+%H:%M:%S')] Dashboard done" >> $LOG

# Step 7: Health check
echo "[$(date '+%H:%M:%S')] Step 7: Health check..." >> $LOG
sleep 10
ssh -o StrictHostKeyChecking=no $CONTABO "curl -sf http://localhost:8100/api/v1/health | python3 -m json.tool" >> $LOG 2>&1
echo "[$(date '+%H:%M:%S')] === ALL DONE ===" >> $LOG
