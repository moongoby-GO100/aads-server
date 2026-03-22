#!/bin/bash
# AADS 호스트 레벨 Watchdog — 크론 1분마다 실행
# aads-server 컨테이너가 내부 Healer도 포함하므로, 컨테이너 자체 장애 시 외부 감시 필요

COMPOSE_DIR="/root/aads/aads-server"
HEALTH_URL="http://localhost:8100/api/v1/health"

# .env에서 텔레그램 변수 로드
if [[ -f "${COMPOSE_DIR}/.env" ]]; then
    TELEGRAM_BOT_TOKEN=$(grep -oP '^TELEGRAM_BOT_TOKEN=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
    TELEGRAM_CHAT_ID=$(grep -oP '^TELEGRAM_CHAT_ID=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
fi

notify() {
    local msg="$1"
    logger "aads-watchdog: ${msg}"
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -sf -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_CHAT_ID}" \
            -d text="🔧 [AADS Watchdog] ${msg}" \
            -d parse_mode=HTML >/dev/null 2>&1 || true
    fi
}

STATUS=$(docker inspect aads-server --format '{{.State.Status}}' 2>/dev/null)

case "$STATUS" in
    running)
        # 컨테이너는 running이지만 API가 응답하지 않을 수 있음
        if ! curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
            notify "⚠️ running but unhealthy — supervisorctl restart aads-api"
            docker exec aads-server supervisorctl restart aads-api
        fi
        ;;
    created|exited)
        notify "🚨 컨테이너 상태: ${STATUS} — docker start 실행"
        docker start aads-server
        sleep 5
        if curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
            notify "✅ 복구 성공"
        else
            notify "❌ docker start 후에도 health 실패"
        fi
        ;;
    "")
        notify "🚨 컨테이너 없음 — docker compose up -d --no-deps aads-server"
        cd "$COMPOSE_DIR" && docker compose up -d --no-deps aads-server
        sleep 10
        if curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
            notify "✅ 컨테이너 재생성 + 복구 성공"
        else
            notify "❌ 컨테이너 재생성 후 health 실패"
        fi
        ;;
esac
