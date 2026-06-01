#!/bin/bash
# Disk Recovery Script - 서버68 레거시 데이터 외장볼륨 이전
# 2026-06-01 생성

LOG="/var/log/disk_recovery.log"
EXT="/mnt/volume_sgp1_01/aads-archive"

echo "=== Disk Recovery Start $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"

# 1. /var/www/server (13GB) → 외장볼륨
echo "[1/5] Moving /var/www/server..." >> "$LOG"
mkdir -p "$EXT/var-www-server"
cp -a /var/www/server/* "$EXT/var-www-server/" 2>> "$LOG"
if [ $? -eq 0 ]; then
    rm -r /var/www/server
    mkdir /var/www/server
    echo "[1/5] DONE - /var/www/server moved" >> "$LOG"
else
    echo "[1/5] FAILED - /var/www/server copy failed" >> "$LOG"
fi

# 2. /var/www/aads-public (3.8GB) → 외장볼륨
echo "[2/5] Moving /var/www/aads-public..." >> "$LOG"
mkdir -p "$EXT/var-www-aads-public"
cp -a /var/www/aads-public/* "$EXT/var-www-aads-public/" 2>> "$LOG"
if [ $? -eq 0 ]; then
    rm -r /var/www/aads-public
    mkdir /var/www/aads-public
    echo "[2/5] DONE - /var/www/aads-public moved" >> "$LOG"
else
    echo "[2/5] FAILED" >> "$LOG"
fi

# 3. /root/webapp (5.7GB) → 외장볼륨
echo "[3/5] Moving /root/webapp..." >> "$LOG"
mkdir -p "$EXT/root-webapp"
cp -a /root/webapp/* "$EXT/root-webapp/" 2>> "$LOG"
if [ $? -eq 0 ]; then
    rm -r /root/webapp
    echo "[3/5] DONE - /root/webapp moved" >> "$LOG"
else
    echo "[3/5] FAILED" >> "$LOG"
fi

# 4. 5/31 미압축 백업 삭제 (압축본 존재 시)
echo "[4/5] Checking backup compression..." >> "$LOG"
if [ -f /root/aads/backups/aads_20260531_030003.sql.gz ]; then
    GZ_SIZE=$(stat -c%s /root/aads/backups/aads_20260531_030003.sql.gz 2>/dev/null)
    if [ "$GZ_SIZE" -gt 1000000000 ]; then
        rm -f /root/aads/backups/aads_20260531_030003.sql
        echo "[4/5] DONE - uncompressed .sql removed (gz=${GZ_SIZE})" >> "$LOG"
    else
        echo "[4/5] SKIP - gz too small (${GZ_SIZE}), compression may be in progress" >> "$LOG"
    fi
else
    echo "[4/5] SKIP - gz not found" >> "$LOG"
fi

# 5. 백업 디렉토리를 외장볼륨 symlink으로 전환
echo "[5/5] Setting up backup symlink..." >> "$LOG"
cp -a /root/aads/backups/*.sql.gz /mnt/volume_sgp1_01/aads-backups/ 2>> "$LOG"
echo "[5/5] Backup copies synced to external volume" >> "$LOG"

AFTER=$(df / --output=pcent | tail -1 | tr -d ' %')
echo "=== Disk Recovery End $(date '+%Y-%m-%d %H:%M:%S') - Disk now at ${AFTER}% ===" >> "$LOG"
