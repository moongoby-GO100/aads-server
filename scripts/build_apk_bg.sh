#!/usr/bin/env bash
set -euo pipefail
LOG="/tmp/apk_build.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] APK 빌드 시작" > "$LOG"

cd /root/aads/aads-server/android_agent
bash build_debug_apk.sh >> "$LOG" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] APK 빌드 성공" >> "$LOG"
    ls -lh dist/aads-agent-debug.apk >> "$LOG" 2>&1
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] APK 빌드 실패 (exit=$EXIT_CODE)" >> "$LOG"
fi
