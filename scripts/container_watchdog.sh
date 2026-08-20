#!/bin/bash
set -euo pipefail

# AADS Container Watchdog — Blue/Green 슬롯 상태 감시
# 배경: 2026-08-20 인시던트 — Blue/Green 컨테이너가 동시 force-recreate되어
#       약 26분간 서비스 전체 다운. 조기 감지를 위해 60초 주기로 양쪽 슬롯을 감시한다.
#
# 사용법:
#   nohup bash /root/aads/aads-server/scripts/container_watchdog.sh \
#     >> /root/aads/aads-server/logs/container_watchdog.log 2>&1 &
#
# 알림 정책 (플래핑 방지):
#   - 컨테이너별로 연속 3회(=3분) 비정상 상태가 확인되어야 텔레그램 알림 발송
#   - 정상 복귀 시 알림이 발송된 적 있으면 복구 알림 1회 발송 후 카운터 초기화

ENV_FILE="/root/aads/aads-server/.env"
INTERVAL=60
BLUE_CONTAINER="aads-server"
GREEN_CONTAINER="aads-server-green"
FAIL_THRESHOLD=3

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

# .env에서 텔레그램 토큰 로드 (없어도 스크립트는 계속 동작, 알림만 스킵)
TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
if [ -f "$ENV_FILE" ]; then
  TELEGRAM_BOT_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | tail -1 | cut -d '=' -f2- | tr -d '"'"'"'\r')"
  TELEGRAM_CHAT_ID="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | tail -1 | cut -d '=' -f2- | tr -d '"'"'"'\r')"
fi

send_telegram() {
  local message="$1"
  if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    log "WARN: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정 — 알림 스킵: ${message}"
    return 0
  fi
  if ! curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d chat_id="${TELEGRAM_CHAT_ID}" \
      -d text="${message}" >/dev/null 2>&1; then
    log "ERROR: 텔레그램 전송 실패: ${message}"
  fi
}

get_status() {
  local container="$1"
  docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || echo "not_found"
}

blue_fail_count=0
green_fail_count=0
blue_alerted=0
green_alerted=0

log "container_watchdog 시작 (interval=${INTERVAL}s, blue=${BLUE_CONTAINER}, green=${GREEN_CONTAINER}, threshold=${FAIL_THRESHOLD})"

while true; do
  blue_status="$(get_status "$BLUE_CONTAINER")"
  green_status="$(get_status "$GREEN_CONTAINER")"

  # ── Blue(ACTIVE) 슬롯 감시 ──────────────────────────────────────────
  if [ "$blue_status" != "running" ]; then
    blue_fail_count=$((blue_fail_count + 1))
    log "WARN: ${BLUE_CONTAINER} 상태=${blue_status} (연속 ${blue_fail_count}/${FAIL_THRESHOLD})"
    if [ "$blue_fail_count" -ge "$FAIL_THRESHOLD" ]; then
      send_telegram "🚨 ACTIVE 슬롯 다운: ${BLUE_CONTAINER} (상태=${blue_status})"
      blue_alerted=1
    fi
  else
    if [ "$blue_alerted" -eq 1 ]; then
      send_telegram "✅ 복구됨: ${BLUE_CONTAINER} 정상(running)"
    fi
    blue_fail_count=0
    blue_alerted=0
  fi

  # ── Green(롤백 대상) 슬롯 감시 ──────────────────────────────────────
  if [ "$green_status" != "running" ]; then
    green_fail_count=$((green_fail_count + 1))
    log "WARN: ${GREEN_CONTAINER} 상태=${green_status} (연속 ${green_fail_count}/${FAIL_THRESHOLD})"
    if [ "$green_fail_count" -ge "$FAIL_THRESHOLD" ]; then
      send_telegram "⚠️ 롤백 불가: ${GREEN_CONTAINER} 상태=${green_status}"
      green_alerted=1
    fi
  else
    if [ "$green_alerted" -eq 1 ]; then
      send_telegram "✅ 복구됨: ${GREEN_CONTAINER} 정상(running) — 롤백 가능 상태"
    fi
    green_fail_count=0
    green_alerted=0
  fi

  sleep "$INTERVAL"
done
