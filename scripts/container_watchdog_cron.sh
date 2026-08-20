#!/bin/bash
# AADS Container Watchdog — cron supervisor
# 배경: container_watchdog.sh는 `while true` 데몬이라 cron 직접 등록이 불가능하다.
#       이 래퍼가 5분마다 실행되어 데몬이 죽어 있으면 재기동한다.
#
# crontab 등록:
#   */5 * * * * /root/aads/aads-server/scripts/container_watchdog_cron.sh >> /var/log/aads-container-watchdog-cron.log 2>&1

set -uo pipefail

WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAEMON="${WORKDIR}/scripts/container_watchdog.sh"
LOG_DIR="${WORKDIR}/logs"
LOG_FILE="${LOG_DIR}/container_watchdog.log"
PID_FILE="${WORKDIR}/.container_watchdog.pid"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

if [ ! -f "$DAEMON" ]; then
  echo "[$(stamp)] ERROR: 데몬 스크립트 없음: ${DAEMON}"
  exit 1
fi

mkdir -p "$LOG_DIR"

# 1) PID 파일 기준 생존 확인
if [ -f "$PID_FILE" ]; then
  OLD_PID="$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || echo '')"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    exit 0
  fi
fi

# 2) PID 파일이 없거나 죽었어도 중복 프로세스가 있으면 그대로 사용
EXISTING_PID="$(pgrep -f "bash ${DAEMON}" | head -1 || true)"
if [ -n "$EXISTING_PID" ]; then
  echo "$EXISTING_PID" > "$PID_FILE"
  exit 0
fi

# 3) 로그 로테이션 (10MB 초과 시)
if [ -f "$LOG_FILE" ]; then
  LOG_SIZE="$(stat -c '%s' "$LOG_FILE" 2>/dev/null || echo 0)"
  if [ "$LOG_SIZE" -gt 10485760 ]; then
    mv -f "$LOG_FILE" "${LOG_FILE}.1"
  fi
fi

# 4) 기동
nohup bash "$DAEMON" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
echo "[$(stamp)] container_watchdog 기동 (pid=${NEW_PID})"
exit 0
