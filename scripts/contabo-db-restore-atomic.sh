#!/bin/bash
# Contabo DB 복원 — atomic swap 방식
# 이 스크립트는 Contabo 서버에서 실행됨
# 사전 조건: /tmp/aads_sync.dump 가 이미 존재해야 함

LOG="/tmp/contabo-db-restore.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] atomic restore start" >> $LOG

# 1. Copy dump into postgres container
docker cp /tmp/aads_sync.dump aads-postgres:/tmp/aads_sync.dump

# 2. Drop old temp DB if exists, create fresh one
docker exec aads-postgres psql -U aads -d postgres -c "DROP DATABASE IF EXISTS aads_tmp" 2>> $LOG
docker exec aads-postgres createdb -U aads aads_tmp 2>> $LOG

# 3. Restore into temp DB (not touching live DB at all)
docker exec aads-postgres pg_restore -U aads -d aads_tmp --no-owner -j 2 /tmp/aads_sync.dump 2>> $LOG

# 4. Verify restore — chat_messages must have data
COUNT=$(docker exec aads-postgres psql -U aads -d aads_tmp -t -c "SELECT count(*) FROM chat_messages" 2>/dev/null | tr -d ' ')
if [ -z "$COUNT" ] || [ "$COUNT" -lt 1000 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ABORT: chat_messages=$COUNT (expected 1000+)" >> $LOG
  docker exec aads-postgres psql -U aads -d postgres -c "DROP DATABASE IF EXISTS aads_tmp" 2>> $LOG
  echo "FAIL:count=$COUNT"
  exit 1
fi

# 5. Atomic swap: terminate → rename → cleanup
docker exec aads-postgres psql -U aads -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'aads' AND pid <> pg_backend_pid();" >> $LOG 2>&1
docker exec aads-postgres psql -U aads -d postgres -c "DROP DATABASE IF EXISTS aads_old;" >> $LOG 2>&1
docker exec aads-postgres psql -U aads -d postgres -c "ALTER DATABASE aads RENAME TO aads_old;" >> $LOG 2>&1
docker exec aads-postgres psql -U aads -d postgres -c "ALTER DATABASE aads_tmp RENAME TO aads;" >> $LOG 2>&1
docker exec aads-postgres psql -U aads -d postgres -c "DROP DATABASE IF EXISTS aads_old;" >> $LOG 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] atomic restore done: chat_messages=$COUNT" >> $LOG
echo "OK:count=$COUNT"
exit 0
