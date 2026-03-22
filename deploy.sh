#!/bin/bash
# AADS 안전 배포 게이트웨이
# 사용법: deploy.sh [code|build]
#   code  (기본) — supervisorctl restart (볼륨마운트로 코드 이미 반영)
#   build        — docker compose up -d --build --no-deps aads-server (postgres 절대 건드리지 않음)

set -euo pipefail

MODE="${1:-code}"
COMPOSE_DIR="/root/aads/aads-server"
HEALTH_URL="http://localhost:8100/api/v1/health"
MAX_WAIT=30
INTERVAL=2

# 텔레그램 알림 (환경변수 있으면 발송)
notify() {
    local msg="$1"
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -sf -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_CHAT_ID}" \
            -d text="🚀 [AADS Deploy] ${msg}" \
            -d parse_mode=HTML >/dev/null 2>&1 || true
    fi
}

# .env에서 텔레그램 변수 로드
if [[ -f "${COMPOSE_DIR}/.env" ]]; then
    export TELEGRAM_BOT_TOKEN=$(grep -oP '^TELEGRAM_BOT_TOKEN=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
    export TELEGRAM_CHAT_ID=$(grep -oP '^TELEGRAM_CHAT_ID=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
fi

echo "[deploy.sh] mode=${MODE} at $(date '+%Y-%m-%d %H:%M:%S')"

case "$MODE" in
    code)
        echo "[deploy.sh] supervisorctl restart aads-api (코드 반영)"
        docker exec aads-server supervisorctl restart aads-api
        ;;
    build)
        echo "[deploy.sh] docker compose up -d --build --no-deps aads-server"
        echo "[deploy.sh] ⚠️  postgres 컨테이너는 건드리지 않습니다"
        PG_ID_BEFORE=$(docker inspect aads-postgres --format '{{.Id}}' 2>/dev/null || echo "N/A")
        cd "$COMPOSE_DIR"
        docker compose up -d --build --no-deps aads-server
        PG_ID_AFTER=$(docker inspect aads-postgres --format '{{.Id}}' 2>/dev/null || echo "N/A")
        if [[ "$PG_ID_BEFORE" != "$PG_ID_AFTER" ]]; then
            notify "⚠️ CRITICAL: postgres 컨테이너 ID 변경됨! before=${PG_ID_BEFORE:0:12} after=${PG_ID_AFTER:0:12}"
            echo "[deploy.sh] ⚠️ CRITICAL: postgres 컨테이너 ID가 변경됨!"
        fi
        ;;
    *)
        echo "[deploy.sh] ERROR: 알 수 없는 모드 '$MODE'. code 또는 build 사용"
        exit 1
        ;;
esac

# Health check polling
echo "[deploy.sh] Health check 시작 (최대 ${MAX_WAIT}초)..."
elapsed=0
while [[ $elapsed -lt $MAX_WAIT ]]; do
    sleep "$INTERVAL"
    elapsed=$((elapsed + INTERVAL))
    if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
        echo "[deploy.sh] ✅ Health OK (${elapsed}초)"
        notify "✅ 배포 완료 (mode=${MODE}, ${elapsed}초)"
        exit 0
    fi
    echo "[deploy.sh] 대기중... ${elapsed}/${MAX_WAIT}초"
done

echo "[deploy.sh] ❌ Health check 실패 (${MAX_WAIT}초 초과)"
notify "❌ 배포 실패: Health check ${MAX_WAIT}초 초과 (mode=${MODE})"
exit 1
