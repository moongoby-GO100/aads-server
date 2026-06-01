#!/bin/bash
# AADS 디스크 자동 정리 v2 — 매일 04:00 실행
# 변경: 2026-06-01 | 백업 관리 + Docker 강화 + 갤러리 정리 + 알림

LOG="/root/aads/logs/disk_cleanup.log"
BACKUP_SRC="/root/aads/backups"
BACKUP_DST="/mnt/volume_sgp1_01/aads-backups"
GALLERY="/root/aads/aads-server/app/static/gallery"
TELEGRAM_SCRIPT="/root/aads/aads-server/scripts/send_disk_alert.sh"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 디스크 정리 v2 시작 ===" >> "$LOG"
BEFORE=$(df / --output=pcent | tail -1 | tr -d ' %')

# 1. 백업 관리: 외장볼륨으로 동기화 + 루트 보관 3일
echo "[BACKUP] 외장볼륨 동기화" >> "$LOG"
mkdir -p "$BACKUP_DST"
cp -n "$BACKUP_SRC"/*.sql.gz "$BACKUP_DST/" 2>/dev/null
# 루트에서 3일 초과 백업 삭제
find "$BACKUP_SRC" -name "*.sql.gz" -mtime +3 -delete 2>/dev/null
find "$BACKUP_SRC" -name "*.sql" -mtime +1 -delete 2>/dev/null
echo "[BACKUP] 루트 3일 보관, 외장볼륨 동기화 완료" >> "$LOG"

# 2. 외장볼륨 백업 보관: 30일 초과 삭제
find "$BACKUP_DST" -name "*.sql.gz" -mtime +30 -delete 2>/dev/null
echo "[BACKUP-EXT] 외장볼륨 30일 보관" >> "$LOG"

# 3. Docker 정리 (강화)
echo "[DOCKER] 미사용 리소스 정리" >> "$LOG"
docker image prune -af --filter "until=48h" >> "$LOG" 2>&1
docker builder prune -af --filter "until=48h" >> "$LOG" 2>&1
docker container prune -f >> "$LOG" 2>&1
docker volume prune -f >> "$LOG" 2>&1

# 4. 로그 정리
find /var/log -name "*.log" -mtime +7 -size +10M -delete 2>/dev/null
find /var/log -name "*.log.*" -mtime +14 -delete 2>/dev/null
find /root/aads/logs -name "*.log" -mtime +14 -delete 2>/dev/null
find /root/.genspark/logs -name "*.log" -mtime +7 -delete 2>/dev/null
echo "[LOG] 로그 정리 완료" >> "$LOG"

# 5. 캐시 정리
npm cache clean --force 2>/dev/null
pip cache purge 2>/dev/null
find /tmp -maxdepth 1 -name "pip-*" -o -name "npm-*" -mtime +1 -exec rm -r {} + 2>/dev/null
echo "[CACHE] 캐시 정리 완료" >> "$LOG"

# 6. 갤러리 이미지: 60일 초과분 외장볼륨 이전
if [ -d "$GALLERY" ]; then
    OLD_COUNT=$(find "$GALLERY" -type f -mtime +60 | wc -l)
    if [ "$OLD_COUNT" -gt 0 ]; then
        mkdir -p /mnt/volume_sgp1_01/aads-gallery-archive
        find "$GALLERY" -type f -mtime +60 -exec mv {} /mnt/volume_sgp1_01/aads-gallery-archive/ \;
        echo "[GALLERY] ${OLD_COUNT}개 이미지 아카이브" >> "$LOG"
    fi
fi

# 7. Git gc
cd /root/aads/aads-server && git gc --auto --quiet 2>/dev/null
cd /root/aads/aads-dashboard && git gc --auto --quiet 2>/dev/null
echo "[GIT] gc 완료" >> "$LOG"

# 결과 기록 + 알림
AFTER=$(df / --output=pcent | tail -1 | tr -d ' %')
FREED=$((BEFORE - AFTER))
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 정리 완료. ${BEFORE}% → ${AFTER}% (${FREED}%p 회수)" >> "$LOG"

# 85% 이상이면 텔레그램 경고
if [ "$AFTER" -gt 85 ]; then
    echo "[ALERT] 디스크 ${AFTER}%! 추가 정리 필요" >> "$LOG"
fi

# 95% 이상이면 긴급 정리
if [ "$AFTER" -gt 95 ]; then
    echo "[EMERGENCY] 긴급 정리 실행" >> "$LOG"
    docker system prune -af >> "$LOG" 2>&1
    find "$BACKUP_SRC" -name "*.sql.gz" -mtime +1 -delete 2>/dev/null
    find "$BACKUP_SRC" -name "*.sql" -delete 2>/dev/null
fi
