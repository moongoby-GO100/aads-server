#!/bin/bash
# AADS Hot-Reload — gunicorn master에 SIGHUP 전송 (0ms 다운타임)
# [2026-04-05] 생성 — 코드 변경 시 무중단 재시작 표준 방법
# 사용: bash /root/aads/aads-server/scripts/reload-api.sh
# 또는 컨테이너 내부: bash /app/scripts/reload-api.sh

LOG="/var/log/blue_green_deploy.log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] $1" | tee -a "$LOG"; }

PID_FILE="/tmp/gunicorn.pid"

if [ -f "$PID_FILE" ]; then
    # 컨테이너 내부 실행
    MASTER_PID=$(cat "$PID_FILE")
    if ! kill -0 "$MASTER_PID" 2>/dev/null; then
        log "[ERROR] gunicorn 마스터 PID $MASTER_PID 없음"
        exit 1
    fi
    kill -HUP "$MASTER_PID"
    sleep 2
    WORKER_COUNT=$(pgrep -P "$MASTER_PID" 2>/dev/null | wc -l)
    log "[OK] Hot-Reload 완료 — 마스터=$MASTER_PID, 워커=${WORKER_COUNT}개"
else
    # 호스트에서 실행 — 컨테이너 내부로 위임
    docker exec aads-server bash /app/scripts/reload-api.sh
fi
