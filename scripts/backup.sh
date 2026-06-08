#!/bin/bash
# AADS PostgreSQL 백업 — atomic gzip + bounded retention
# cron (claudebot): 0 3 * * * /root/aads/scripts/backup.sh
set -euo pipefail

BACKUP_DIR=/root/aads/backups
EXT_VOL=/mnt/volume_sgp1_01/aads-backups
ROOT_KEEP_DAYS=${AADS_BACKUP_ROOT_KEEP_DAYS:-3}
EXT_KEEP_COUNT=${AADS_BACKUP_EXT_KEEP_COUNT:-2}
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
COMPRESSED_FILE=${BACKUP_DIR}/aads_${TIMESTAMP}.sql.gz
TMP_FILE="${COMPRESSED_FILE}.tmp"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"
}

cleanup_tmp() {
  [ ! -e "$TMP_FILE" ] || rm -f "$TMP_FILE"
}
trap cleanup_tmp EXIT

prune_invalid_backups() {
  find "$BACKUP_DIR" "$EXT_VOL" -maxdepth 1 -type f -name "aads_*.sql.gz" -size 0 -delete 2>/dev/null || true
  for f in "$BACKUP_DIR"/aads_*.sql.gz "$EXT_VOL"/aads_*.sql.gz; do
    [ -f "$f" ] || continue
    gzip -t "$f" 2>/dev/null || {
      log "invalid gzip removed: $f"
      rm -f "$f"
    }
  done
}

prune_external_keep_count() {
  [ -d "$EXT_VOL" ] || return 0
  mapfile -t old_files < <(
    find "$EXT_VOL" -maxdepth 1 -type f -name "aads_*.sql.gz" -printf '%T@ %p\n' \
      | sort -nr \
      | awk -v keep="$EXT_KEEP_COUNT" 'NR > keep {print $2}'
  )
  for f in "${old_files[@]}"; do
    log "external retention remove: $f"
    rm -f "$f"
  done
}

mkdir -p "$BACKUP_DIR" "$EXT_VOL" 2>/dev/null
prune_invalid_backups
prune_external_keep_count

log "backup start: $COMPRESSED_FILE"
docker exec aads-postgres pg_dump -U aads -d aads --no-owner --no-acl 2>/dev/null | gzip -1 > "$TMP_FILE"

if [ ! -s "$TMP_FILE" ]; then
  log "backup failed: empty gzip output"
  exit 1
fi
gzip -t "$TMP_FILE"
mv "$TMP_FILE" "$COMPRESSED_FILE"
trap - EXIT

log "backup done: $COMPRESSED_FILE ($(du -sh "$COMPRESSED_FILE" | cut -f1))"

find "$BACKUP_DIR" -maxdepth 1 -type f -name "aads_*.sql.gz" -mtime +"$ROOT_KEEP_DAYS" -exec mv {} "$EXT_VOL"/ \; 2>/dev/null || true
for f in "$BACKUP_DIR"/aads_*.sql; do
  [ -f "$f" ] || continue
  gzip -1f "$f" 2>/dev/null || true
done

prune_invalid_backups
prune_external_keep_count
log "disk root=$(df -h / | tail -1 | awk '{print $5}') ext=$(df -h /mnt/volume_sgp1_01 | tail -1 | awk '{print $5}')"
