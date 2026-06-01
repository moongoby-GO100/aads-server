#!/bin/bash
# Server68 디스크 긴급 정리 스크립트
# 2026-06-01 인프라 최고관리자 실행
set -e

EXT_VOL="/mnt/volume_sgp1_01/aads-backups"
BACKUP_DIR="/root/aads/backups"
LOG="/tmp/disk_cleanup_$(date +%Y%m%d_%H%M%S).log"

echo "=== 디스크 긴급 정리 시작 $(date) ===" | tee "$LOG"
echo "정리 전:" | tee -a "$LOG"
df -h / | tee -a "$LOG"

# 1. 오래된 백업을 외부 볼륨으로 이동 (May 22~28, 7건)
echo -e "\n--- 1. 백업 외부 볼륨 이동 ---" | tee -a "$LOG"
for f in "$BACKUP_DIR"/aads_202605{22,23,24,25,26,27,28}_*.sql.gz; do
  if [ -f "$f" ]; then
    echo "이동: $(basename $f) ($(du -h "$f" | cut -f1))" | tee -a "$LOG"
    mv "$f" "$EXT_VOL/"
  fi
done

# 2. 비압축 백업을 gzip 압축 (May 30, 31)
echo -e "\n--- 2. 비압축 백업 gzip 압축 ---" | tee -a "$LOG"
for f in "$BACKUP_DIR"/aads_*.sql; do
  if [ -f "$f" ]; then
    echo "압축: $(basename $f) ($(du -h "$f" | cut -f1))" | tee -a "$LOG"
    gzip "$f"
    echo "완료: $(basename $f).gz ($(du -h "$f.gz" | cut -f1))" | tee -a "$LOG"
  fi
done

# 3. 개발 도구 캐시 삭제 (서비스 무관, 자동 재생성)
echo -e "\n--- 3. 개발 도구 캐시 정리 ---" | tee -a "$LOG"
for d in /root/.cursor-server /root/.codex /root/.codex-relay /root/.codex-chrome-profile /root/.codex-chrome-profile-test; do
  if [ -d "$d" ]; then
    SIZE=$(du -sh "$d" | cut -f1)
    echo "삭제: $d ($SIZE)" | tee -a "$LOG"
    rm -rf "$d"
  fi
done

# 4. 미사용 런타임 삭제
echo -e "\n--- 4. 미사용 런타임 정리 ---" | tee -a "$LOG"
for d in /root/.rustup; do
  if [ -d "$d" ]; then
    SIZE=$(du -sh "$d" | cut -f1)
    echo "삭제: $d ($SIZE)" | tee -a "$LOG"
    rm -rf "$d"
  fi
done

# 5. npm/pip 캐시 정리
echo -e "\n--- 5. 캐시 정리 ---" | tee -a "$LOG"
npm cache clean --force 2>/dev/null && echo "npm 캐시 정리 완료" | tee -a "$LOG"
pip cache purge 2>/dev/null && echo "pip 캐시 정리 완료" | tee -a "$LOG"

# 6. 대형 로그 truncate
echo -e "\n--- 6. 대형 로그 정리 ---" | tee -a "$LOG"
if [ -f /var/log/gallery_sync.log ]; then
  SIZE=$(du -h /var/log/gallery_sync.log | cut -f1)
  echo "truncate: /var/log/gallery_sync.log ($SIZE)" | tee -a "$LOG"
  : > /var/log/gallery_sync.log
fi

echo -e "\n=== 정리 완료 $(date) ===" | tee -a "$LOG"
echo "정리 후:" | tee -a "$LOG"
df -h / | tee -a "$LOG"
echo "외부 볼륨:" | tee -a "$LOG"
df -h /mnt/volume_sgp1_01/ | tee -a "$LOG"
