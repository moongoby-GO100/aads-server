#!/bin/bash
LOG="/tmp/contabo-db-restore.log"
CONTABO="root@5.104.86.116"
exec > $LOG 2>&1

echo "[$(date '+%H:%M:%S')] Fresh DB dump..."
docker exec aads-postgres pg_dump -U aads -d aads -Fc -f /tmp/aads_fresh.dump
docker cp aads-postgres:/tmp/aads_fresh.dump /tmp/aads_fresh.dump
echo "[$(date '+%H:%M:%S')] Dump done ($(du -h /tmp/aads_fresh.dump | cut -f1))"

echo "[$(date '+%H:%M:%S')] Transfer to Contabo..."
scp -o StrictHostKeyChecking=no /tmp/aads_fresh.dump $CONTABO:/tmp/aads_fresh.dump
echo "[$(date '+%H:%M:%S')] Transfer done"

echo "[$(date '+%H:%M:%S')] Drop and recreate DB on Contabo..."
ssh -o StrictHostKeyChecking=no $CONTABO "docker exec aads-postgres dropdb -U aads --if-exists aads_tmp 2>/dev/null; docker exec aads-postgres createdb -U aads aads_tmp"
ssh -o StrictHostKeyChecking=no $CONTABO "docker cp /tmp/aads_fresh.dump aads-postgres:/tmp/aads_fresh.dump"
ssh -o StrictHostKeyChecking=no $CONTABO "docker exec aads-postgres pg_restore -U aads -d aads_tmp --no-owner -j 2 /tmp/aads_fresh.dump" 2>&1 | tail -5

echo "[$(date '+%H:%M:%S')] Swap databases..."
ssh -o StrictHostKeyChecking=no $CONTABO "docker exec aads-postgres psql -U aads -d postgres -c 'SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='\''aads'\'' AND pid <> pg_backend_pid();' 2>/dev/null"
ssh -o StrictHostKeyChecking=no $CONTABO "docker exec aads-postgres psql -U aads -d postgres -c 'DROP DATABASE IF EXISTS aads_old;'"
ssh -o StrictHostKeyChecking=no $CONTABO "docker exec aads-postgres psql -U aads -d postgres -c 'ALTER DATABASE aads RENAME TO aads_old;'"
ssh -o StrictHostKeyChecking=no $CONTABO "docker exec aads-postgres psql -U aads -d postgres -c 'ALTER DATABASE aads_tmp RENAME TO aads;'"
echo "[$(date '+%H:%M:%S')] Swap done"

echo "[$(date '+%H:%M:%S')] Restart aads-server..."
ssh -o StrictHostKeyChecking=no $CONTABO "docker restart aads-server aads-server-green" 2>&1 | tail -3

echo "[$(date '+%H:%M:%S')] Verify..."
sleep 10
ssh -o StrictHostKeyChecking=no $CONTABO "docker exec aads-postgres psql -U aads -d aads -t -c 'SELECT count(*) FROM chat_messages'"
echo "[$(date '+%H:%M:%S')] === RESTORE DONE ==="
