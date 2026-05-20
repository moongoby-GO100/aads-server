#!/bin/bash
# AADS 서버68 → Contabo 도쿄 완전 동기화 스크립트
# 코드 rsync + 변경 감지 시 Docker 리빌드 + 1시간마다 DB 동기화

CONTABO="root@5.104.86.116"
SSH_KEY="/root/.ssh/id_ed25519"
LOG="/tmp/contabo-sync.log"
HASH_FILE="/tmp/contabo-sync-hash"
DASHBOARD_HASH_FILE="/tmp/contabo-dashboard-sync-hash"
DB_SYNC_MARKER="/tmp/contabo-db-sync-last"
LOCKFILE="/tmp/contabo-sync.lock"

# 동시 실행 방지
if [ -f "$LOCKFILE" ]; then
  LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null || echo "")
  if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
    LOCK_CMD=$(ps -p "$LOCK_PID" -o args= 2>/dev/null || echo "")
    if echo "$LOCK_CMD" | grep -q "sync-to-contabo.sh"; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] skip (locked by PID $LOCK_PID)" >> "$LOG"
      exit 0
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] stale lock reused by another process (PID $LOCK_PID), clearing" >> "$LOG"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] stale lock cleared (PID ${LOCK_PID:-unknown})" >> "$LOG"
  fi
  rm -f "$LOCKFILE"
fi
echo $$ > "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

echo "[$(date '+%Y-%m-%d %H:%M:%S')] sync start" >> "$LOG"

# --- 1. 코드 동기화 (aads-server) ---
rsync -az --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='.env.local' \
  --exclude='node_modules' \
  --exclude='dist/' \
  --exclude='*.egg-info' \
  --exclude='app.db' \
  --exclude='aads.db' \
  --exclude='RESULT*.md' \
  --exclude='RUNNER_TEST*.md' \
  --exclude='.bak_aads*' \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10" \
  /root/aads/aads-server/ $CONTABO:/root/aads/aads-server/ 2>> "$LOG"

# --- 2. 대시보드 동기화 ---
rsync -az --delete \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='.next' \
  --exclude='.env.local' \
  --exclude='.active_*' \
  --exclude='*.bak_aads*' \
  --exclude='*.bak_*' \
  --exclude='build.log' \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10" \
  /root/aads/aads-dashboard/ $CONTABO:/root/aads/aads-dashboard/ 2>> "$LOG"

# --- 3. 문서 동기화 ---
rsync -az \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10" \
  /root/aads/aads-docs/ $CONTABO:/root/aads/aads-docs/ 2>> "$LOG"

# --- 4. 백엔드 변경 감지 → API 핫리로드 ---
AFTER_HASH=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 $CONTABO "find /root/aads/aads-server/app -name '*.py' -type f -exec md5sum {} + 2>/dev/null | sort | md5sum | cut -d' ' -f1" 2>/dev/null)
OLD_HASH=$(cat $HASH_FILE 2>/dev/null || echo "none")

if [ "$AFTER_HASH" != "$OLD_HASH" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] code changed, reloading API..." >> "$LOG"
  ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 $CONTABO "docker exec aads-server bash /app/scripts/reload-api.sh" >> "$LOG" 2>&1
  echo "$AFTER_HASH" > $HASH_FILE
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] API reloaded" >> "$LOG"
fi

# --- 5. 대시보드 변경 감지 → Contabo Blue-Green 배포 ---
DASHBOARD_HASH=$(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 $CONTABO "cd /root/aads/aads-dashboard && find . \
  \( -path './.git' -o -path './.git/*' -o -path './node_modules' -o -path './node_modules/*' -o -path './.next' -o -path './.next/*' -o -path './docs' -o -path './docs/*' -o -path './reports' -o -path './reports/*' \) -prune \
  -o -type f ! -name 'tsconfig.tsbuildinfo' ! -name '.active_port' ! -name '.active_container' ! -name 'build.log' ! -name 'HANDOVER.md' ! -name '*.bak_aads*' ! -name '*.bak_*' -print0 | sort -z | xargs -0 md5sum 2>/dev/null | md5sum | cut -d' ' -f1" 2>/dev/null)
DASHBOARD_OLD_HASH=$(cat $DASHBOARD_HASH_FILE 2>/dev/null || echo "none")

if [ -n "$DASHBOARD_HASH" ] && [ "$DASHBOARD_HASH" != "$DASHBOARD_OLD_HASH" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] dashboard changed, running Contabo blue-green deploy..." >> "$LOG"
  if ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 $CONTABO "cd /root/aads/aads-dashboard && DASHBOARD_EXTERNAL_HEALTH_URL=http://127.0.0.1/login AADS_DASHBOARD_QA_STRICT=false bash deploy.sh" >> "$LOG" 2>&1; then
    echo "$DASHBOARD_HASH" > $DASHBOARD_HASH_FILE
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] dashboard blue-green deploy done" >> "$LOG"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] dashboard blue-green deploy FAILED — kept current dashboard" >> "$LOG"
  fi
fi

# --- 6. DB 동기화 (1시간마다, atomic swap) ---
LAST_DB=$(cat $DB_SYNC_MARKER 2>/dev/null || echo "0")
NOW=$(date +%s)
DIFF=$(( NOW - LAST_DB ))

if [ $DIFF -gt 3600 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] DB sync start (atomic swap)..." >> "$LOG"
  docker exec aads-postgres pg_dump -U aads -d aads -Fc -f /tmp/aads_sync.dump 2>> "$LOG"
  docker cp aads-postgres:/tmp/aads_sync.dump /tmp/aads_sync.dump >> "$LOG" 2>&1
  scp -o StrictHostKeyChecking=no -i $SSH_KEY /tmp/aads_sync.dump $CONTABO:/tmp/aads_sync.dump >> "$LOG" 2>&1
  RESULT=$(ssh -o StrictHostKeyChecking=no -i $SSH_KEY $CONTABO "bash /root/aads/aads-server/scripts/contabo-db-restore-atomic.sh" 2>> "$LOG")
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] DB sync result: $RESULT" >> "$LOG"
  if echo "$RESULT" | grep -q "^OK:"; then
    echo "$NOW" > $DB_SYNC_MARKER
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] DB sync done (atomic swap OK)" >> "$LOG"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] DB sync FAILED — kept current DB" >> "$LOG"
  fi
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] sync done" >> "$LOG"
