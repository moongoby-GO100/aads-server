#!/bin/bash
# AADS 호스트 레벨 Watchdog — 크론 1분마다 실행
# 3단계 검증: 컨테이너 상태 → API Health → DB 채팅 기능

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

# ── Layer 0: 의존 컨테이너 (postgres, redis) 먼저 확인 ──
for DEP in aads-postgres aads-redis aads-socket-proxy aads-litellm aads-dashboard; do
    DEP_STATUS=$(docker inspect "$DEP" --format '{{.State.Status}}' 2>/dev/null)
    case "$DEP_STATUS" in
        running) ;;
        created|exited)
            notify "🚨 ${DEP} 상태: ${DEP_STATUS} — docker start 실행"
            docker start "$DEP"
            sleep 3
            ;;
        "")
            notify "🚨 ${DEP} 컨테이너 없음 — docker compose up"
            cd "$COMPOSE_DIR" && docker compose up -d --no-deps "$DEP"
            sleep 5
            ;;
    esac
done

STATUS=$(docker inspect aads-server --format '{{.State.Status}}' 2>/dev/null)

case "$STATUS" in
    running)
        # Layer 1: API Health (3x retry, 30s timeout, 5min cooldown)
        COOLDOWN_FILE="/tmp/aads-watchdog-restart.lock"
        HEALTH_OK=false
        for i in 1 2 3; do
            if curl -sf --max-time 30 "$HEALTH_URL" >/dev/null 2>&1; then
                HEALTH_OK=true
                break
            fi
            [ "$i" -lt 3 ] && sleep 5
        done

        if [ "$HEALTH_OK" = false ]; then
            if [ -f "$COOLDOWN_FILE" ]; then
                LAST=$(stat -c %Y "$COOLDOWN_FILE" 2>/dev/null || echo 0)
                NOW=$(date +%s)
                if [ $((NOW - LAST)) -lt 300 ]; then
                    logger "aads-watchdog: cooldown active -- skip"
                    exit 0
                fi
            fi
            API_STATE=$(docker exec aads-server supervisorctl status aads-api 2>/dev/null | awk '{print $2}')
            if [ "$API_STATE" = "STARTING" ] || [ "$API_STATE" = "STOPPING" ]; then
                logger "aads-watchdog: aads-api state=$API_STATE -- skip restart"
                exit 0
            fi
            touch "$COOLDOWN_FILE"
            notify "unhealthy 3x -- supervisorctl restart aads-api"
            docker exec aads-server supervisorctl restart aads-api
            exit 0
        fi

        # Layer 2: DB 채팅 기능 테스트 (5분마다 = 매 5번째 실행)
        MINUTE=$(date +%M)
        if (( MINUTE % 5 == 0 )); then
            DB_TEST=$(docker exec aads-postgres psql -U aads -d aads -t -A -c "
              SELECT 'DB_OK' WHERE EXISTS(
                SELECT 1 FROM information_schema.columns
                WHERE table_name='chat_messages' AND column_name='content'
              );
            " 2>/dev/null || echo "DB_FAIL")

            if [[ "$DB_TEST" != *"DB_OK"* ]]; then
                notify "🚨 DB 채팅 테이블 이상 감지 — 스키마 확인 필요"
            fi

            # streaming_placeholder 잔존 확인
            STALE=$(docker exec aads-postgres psql -U aads -d aads -t -A -c "
              SELECT count(*) FROM chat_messages
              WHERE intent = 'streaming_placeholder'
              AND created_at < NOW() - interval '3 minutes';
            " 2>/dev/null || echo "0")

            if [[ "$STALE" -gt 0 ]] 2>/dev/null; then
                docker exec aads-postgres psql -U aads -d aads -q -c "
                  DELETE FROM chat_messages WHERE intent = 'streaming_placeholder'
                  AND created_at < NOW() - interval '3 minutes';
                " 2>/dev/null
                notify "⚠️ stale placeholder ${STALE}건 자동 정리"
            fi
        fi
        ;;
    created|exited)
        notify "🚨 컨테이너 상태: ${STATUS} — docker start 실행"
        docker start aads-server
        sleep 8
        # V2: 복구 후 health + 채팅 INSERT 테스트
        if curl -sf --max-time 30 "$HEALTH_URL" >/dev/null 2>&1; then
            INSERT_TEST=$(docker exec aads-postgres psql -U aads -d aads -t -A -c "
              WITH ins AS (INSERT INTO chat_messages (session_id, role, content) SELECT id, 'user', '_watchdog_test_' FROM chat_sessions LIMIT 1 RETURNING id),
              del AS (DELETE FROM chat_messages WHERE id IN (SELECT id FROM ins))
              SELECT 'OK' FROM ins LIMIT 1;
            " 2>/dev/null || echo "FAIL")
            if echo "$INSERT_TEST" | grep -q "OK"; then
                notify "✅ 복구 성공 (health + 채팅 INSERT 검증)"
            else
                notify "⚠️ 복구됨 but 채팅 INSERT 실패 — DB 스키마 확인 필요"
            fi
        else
            notify "❌ docker start 후에도 health 실패"
        fi
        ;;
    "")
        notify "🚨 컨테이너 없음 — docker compose up -d --no-deps aads-server"
        cd "$COMPOSE_DIR" && docker compose up -d --no-deps aads-server
        sleep 12
        # V2: 복구 후 health + 채팅 INSERT 테스트
        if curl -sf --max-time 30 "$HEALTH_URL" >/dev/null 2>&1; then
            INSERT_TEST=$(docker exec aads-postgres psql -U aads -d aads -t -A -c "
              WITH ins AS (INSERT INTO chat_messages (session_id, role, content) SELECT id, 'user', '_watchdog_test_' FROM chat_sessions LIMIT 1 RETURNING id),
              del AS (DELETE FROM chat_messages WHERE id IN (SELECT id FROM ins))
              SELECT 'OK' FROM ins LIMIT 1;
            " 2>/dev/null || echo "FAIL")
            if echo "$INSERT_TEST" | grep -q "OK"; then
                notify "✅ 재생성 + 복구 성공 (health + 채팅 INSERT 검증)"
            else
                notify "⚠️ 재생성됨 but 채팅 INSERT 실패"
            fi
        else
            notify "❌ 컨테이너 재생성 후 health 실패"
        fi
        ;;
esac
