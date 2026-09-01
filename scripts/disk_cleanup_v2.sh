#!/bin/bash
# AADS 디스크 자동 정리 v2 — 매일 04:00 실행
# 변경: 2026-06-01 | 백업 관리 + Docker 강화 + 갤러리 정리 + 알림

LOG="/root/aads/logs/disk_cleanup.log"
BACKUP_SRC="/root/aads/backups"
BACKUP_DST="/mnt/volume_sgp1_01/aads-backups"
GALLERY="/root/aads/aads-server/app/static/gallery"
TELEGRAM_SCRIPT="/root/aads/aads-server/scripts/send_disk_alert.sh"

mkdir -p "$(dirname "$LOG")"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === 디스크 정리 v2 시작 ===" >> "$LOG"
BEFORE=$(df / --output=pcent | tail -1 | tr -d ' %')

# 1. 백업 관리: 외장볼륨으로 동기화 + 루트 보관 3일
echo "[BACKUP] 외장볼륨 동기화" >> "$LOG"
mkdir -p "$BACKUP_DST"
find "$BACKUP_SRC" "$BACKUP_DST" -maxdepth 1 -type f -name "aads_*.sql.gz" -size 0 -delete 2>/dev/null
for f in "$BACKUP_SRC"/aads_*.sql.gz "$BACKUP_DST"/aads_*.sql.gz; do
    [ -f "$f" ] || continue
    gzip -t "$f" 2>/dev/null || {
        echo "[BACKUP] 손상 gzip 삭제: $f" >> "$LOG"
        rm -f "$f"
    }
done
for f in "$BACKUP_SRC"/aads_*.sql.gz; do
    [ -f "$f" ] || continue
    cp -n "$f" "$BACKUP_DST/" 2>/dev/null || true
done
# 루트에서 3일 초과 백업 삭제
find "$BACKUP_SRC" -name "*.sql.gz" -mtime +3 -delete 2>/dev/null
find "$BACKUP_SRC" -name "*.sql" -mtime +1 -delete 2>/dev/null
echo "[BACKUP] 루트 3일 보관, 외장볼륨 동기화 완료" >> "$LOG"

# 2. 외장볼륨 백업 보관: 최신 2개만 유지
find "$BACKUP_DST" -maxdepth 1 -type f -name "aads_*.sql.gz" -printf '%T@ %p\n' \
    | sort -nr \
    | awk 'NR > 2 {print $2}' \
    | xargs -r rm -f
echo "[BACKUP-EXT] 외장볼륨 최신 2개 보관" >> "$LOG"

# 3. Docker 정리
# 운영 원칙:
# - 실행 중 컨테이너가 참조하는 image digest는 삭제하지 않는다.
# - DB/Redis/generated-media 같은 운영 volume은 절대 prune하지 않는다.
# - tagged 이미지가 쌓이는 blue/green 특성상 dangling prune만으로는 부족하므로
#   AADS 계열 과거 태그 중 미사용 이미지만 선별 삭제한다.
echo "[DOCKER] 미사용 이미지/빌드캐시 정리" >> "$LOG"
ACTIVE_IMAGE_IDS="$(docker ps -q | xargs -r docker inspect --format '{{.Image}}' | sort -u || true)"

is_active_image() {
    local image_ref="$1"
    local image_id
    image_id="$(docker image inspect --format '{{.Id}}' "$image_ref" 2>/dev/null || true)"
    [ -n "$image_id" ] && printf '%s\n' "$ACTIVE_IMAGE_IDS" | grep -qx "$image_id"
}

prune_project_images() {
    docker image ls --format '{{.Repository}}:{{.Tag}}' \
        | grep -E '^(aads-server|aads-dashboard|aads-server-aads-|aads-dashboard-aads-|aads-server-yeoljeong-finance)' \
        | while read -r image_ref; do
            [ -n "$image_ref" ] || continue
            if is_active_image "$image_ref"; then
                echo "[DOCKER] keep active image: $image_ref" >> "$LOG"
                continue
            fi
            echo "[DOCKER] remove unused project image: $image_ref" >> "$LOG"
            docker image rm "$image_ref" >> "$LOG" 2>&1 || true
        done
}

docker image prune -f --filter "dangling=true" >> "$LOG" 2>&1 || true
docker builder prune -af --filter "until=168h" >> "$LOG" 2>&1 || true
docker container prune -f >> "$LOG" 2>&1 || true
prune_project_images
docker pull node:20-slim >> "$LOG" 2>&1 || true

# 4. 로그 정리
find /var/log -name "*.log" -mtime +7 -size +10M -delete 2>/dev/null
find /var/log -name "*.log.*" -mtime +14 -delete 2>/dev/null
find /root/aads/logs -name "*.log" -mtime +14 -delete 2>/dev/null
find /root/.genspark/logs -name "*.log" -mtime +7 -delete 2>/dev/null
journalctl --vacuum-size=1G >> "$LOG" 2>&1 || true
echo "[LOG] 로그 정리 완료" >> "$LOG"

# 5. 캐시 정리
npm cache clean --force 2>/dev/null
pip cache purge 2>/dev/null
find /tmp -maxdepth 1 \( -name "pip-*" -o -name "npm-*" \) -mtime +1 -exec rm -r {} + 2>/dev/null
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
    docker image prune -f --filter "dangling=true" >> "$LOG" 2>&1 || true
    docker builder prune -af --filter "until=24h" >> "$LOG" 2>&1 || true
    prune_project_images
    find "$BACKUP_SRC" -name "*.sql.gz" -mtime +1 -delete 2>/dev/null
    find "$BACKUP_SRC" -name "*.sql" -delete 2>/dev/null
fi
