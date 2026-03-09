#!/usr/bin/env bash
# AADS PostgreSQL 일일 백업 — 메모리 레이어·세션 노트·관찰 데이터 복원용
# docker exec 사용으로 호스트 libpq/SCRAM 버전 이슈 회피
#
# 사용:
#   /root/aads/aads-server/scripts/backup_postgres.sh
# cron (일 1회 03:00 KST):
#   0 3 * * * /root/aads/aads-server/scripts/backup_postgres.sh >> /root/aads/logs/backup.log 2>&1
#
# 환경변수 (선택):
#   AADS_BACKUP_DIR     출력 디렉터리 (기본: /root/aads/backups)
#   AADS_RETENTION_DAYS 보관 일수 (기본: 7)
#   AADS_PG_CONTAINER   Postgres 컨테이너명 (기본: aads-postgres)

set -euo pipefail

BACKUP_DIR="${AADS_BACKUP_DIR:-/root/aads/backups}"
LOG_DIR="${AADS_BACKUP_LOG_DIR:-/root/aads/logs}"
RETENTION_DAYS="${AADS_RETENTION_DAYS:-7}"
CONTAINER="${AADS_PG_CONTAINER:-aads-postgres}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/aads_${TIMESTAMP}.sql"

mkdir -p "${BACKUP_DIR}" "${LOG_DIR}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 백업 시작: ${BACKUP_FILE}"

if docker exec "${CONTAINER}" pg_dump -U aads -d aads --no-owner --no-acl > "${BACKUP_FILE}" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 백업 완료: ${BACKUP_FILE} ($(du -sh "${BACKUP_FILE}" 2>/dev/null | cut -f1))"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 백업 실패 (컨테이너=${CONTAINER}). 확인: docker ps | grep postgres"
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${RETENTION_DAYS}일 초과 백업 정리 중..."
find "${BACKUP_DIR}" -maxdepth 1 -name "aads_*.sql" -mtime +"${RETENTION_DAYS}" -delete
REMAINING=$(find "${BACKUP_DIR}" -maxdepth 1 -name "aads_*.sql" 2>/dev/null | wc -l)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 정리 완료. 남은 백업: ${REMAINING}개"
