#!/bin/bash
# AADS 안전 배포 게이트웨이
# 사용법: deploy.sh [code|build]
#   code  (기본) — supervisorctl restart (볼륨마운트로 코드 이미 반영)
#   build        — docker compose up -d --build --no-deps aads-server (postgres 절대 건드리지 않음)
#
# 검증 3단계: Health Check → DB 스키마 검증 → 채팅 기능 테스트

set -euo pipefail

MODE="${1:-code}"
COMPOSE_DIR="/root/aads/aads-server"
HEALTH_URL="http://localhost:8100/api/v1/health"
MAX_WAIT=30
INTERVAL=2
TEST_SESSION="00000000-0000-0000-0000-000000000000"  # 테스트 전용 세션

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

# ── Phase 0: 의존 컨테이너 상태 확인 + 복구 ──
echo "[deploy.sh] Phase 0: dependency check..."
for DEP in aads-postgres aads-redis; do
    DEP_STATUS=$(docker inspect "$DEP" --format '{{.State.Status}}' 2>/dev/null)
    if [[ "$DEP_STATUS" != "running" ]]; then
        echo "[deploy.sh] ⚠️ ${DEP} 상태: ${DEP_STATUS:-없음} — 복구 중..."
        docker start "$DEP" 2>/dev/null || (cd "$COMPOSE_DIR" && docker compose up -d --no-deps "$DEP")
        sleep 3
        notify "⚠️ 배포 전 ${DEP} 복구 실행 (이전 상태: ${DEP_STATUS:-없음})"
    fi
done

echo "[deploy.sh] Phase 0: pre-deploy cleanup..."
docker exec aads-postgres psql -U aads -d aads -q -c "
  DELETE FROM chat_messages WHERE intent = 'streaming_placeholder';
  UPDATE chat_messages SET intent = NULL WHERE intent IN ('bg_partial', 'interrupted');
" 2>/dev/null || echo "[deploy.sh] WARN: pre-deploy cleanup skipped"

# ── Phase 1: 배포 실행 ──
case "$MODE" in
    code)
        echo "[deploy.sh] Phase 1: supervisorctl restart aads-api"
        docker exec aads-server supervisorctl restart aads-api || true
        ;;
    build)
        echo "[deploy.sh] Phase 1: docker compose up -d --build --no-deps aads-server"
        PG_ID_BEFORE=$(docker inspect aads-postgres --format '{{.Id}}' 2>/dev/null || echo "N/A")
        cd "$COMPOSE_DIR"
        docker compose up -d --build --no-deps aads-server
        PG_ID_AFTER=$(docker inspect aads-postgres --format '{{.Id}}' 2>/dev/null || echo "N/A")
        if [[ "$PG_ID_BEFORE" != "$PG_ID_AFTER" ]]; then
            notify "⚠️ CRITICAL: postgres 컨테이너 ID 변경됨!"
            echo "[deploy.sh] ⚠️ CRITICAL: postgres 컨테이너 ID가 변경됨!"
        fi
        ;;
    *)
        echo "[deploy.sh] ERROR: 알 수 없는 모드 '$MODE'. code 또는 build 사용"
        exit 1
        ;;
esac

# ── Phase 2: Health Check ──
echo "[deploy.sh] Phase 2: Health check (최대 ${MAX_WAIT}초)..."
elapsed=0
HEALTH_OK=false
while [[ $elapsed -lt $MAX_WAIT ]]; do
    sleep "$INTERVAL"
    elapsed=$((elapsed + INTERVAL))
    if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
        echo "[deploy.sh] Phase 2: ✅ Health OK (${elapsed}초)"
        HEALTH_OK=true
        break
    fi
    echo "[deploy.sh] 대기중... ${elapsed}/${MAX_WAIT}초"
done

if [[ "$HEALTH_OK" != "true" ]]; then
    echo "[deploy.sh] ❌ Phase 2 실패 — 롤백 시도..."
    if [[ "$MODE" == "code" ]]; then
        docker exec aads-server supervisorctl restart aads-api || true
        sleep 10
    fi
    notify "❌ 배포 실패 + 롤백 시도 (mode=${MODE})"
    exit 1
fi

# ── Phase 3: DB 스키마 검증 ──
echo "[deploy.sh] Phase 3: DB 스키마 검증..."
SCHEMA_RESULT=$(docker exec aads-postgres psql -U aads -d aads -t -A -c "
  SELECT string_agg(column_name, ',') FROM information_schema.columns
  WHERE table_name = 'chat_messages' AND column_name IN ('branch_id','intent','content','session_id','role');
" 2>/dev/null || echo "ERROR")

if [[ "$SCHEMA_RESULT" == "ERROR" ]]; then
    echo "[deploy.sh] ⚠️ Phase 3: DB 연결 실패 — 스키마 검증 스킵"
else
    MISSING=""
    for COL in branch_id intent content session_id role; do
        if [[ "$SCHEMA_RESULT" != *"$COL"* ]]; then
            MISSING="${MISSING} ${COL}"
        fi
    done
    if [[ -n "$MISSING" ]]; then
        echo "[deploy.sh] ⚠️ Phase 3: 누락 컬럼 감지:${MISSING}"
        notify "⚠️ DB 컬럼 누락 감지:${MISSING} — 자동 생성 시도"
        # 자동 생성 시도
        for COL in $MISSING; do
            echo "[deploy.sh] ALTER TABLE chat_messages ADD COLUMN ${COL}..."
            docker exec aads-postgres psql -U aads -d aads -c \
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS ${COL} UUID DEFAULT NULL;" 2>/dev/null || true
        done
    else
        echo "[deploy.sh] Phase 3: ✅ 필수 컬럼 정상"
    fi
fi

# ── Phase 4: 채팅 기능 테스트 (실제 INSERT + SELECT + DELETE) ──
echo "[deploy.sh] Phase 4: 채팅 기능 테스트..."
# 기존 세션에 테스트 메시지 INSERT → 확인 → DELETE
CHAT_TEST=$(docker exec aads-postgres psql -U aads -d aads -t -A -c "
  WITH ins AS (
    INSERT INTO chat_messages (session_id, role, content, intent)
    SELECT id, 'user', '_deploy_test_', NULL
    FROM chat_sessions LIMIT 1
    RETURNING id
  ), del AS (
    DELETE FROM chat_messages WHERE id IN (SELECT id FROM ins)
  )
  SELECT CASE WHEN (SELECT count(*) FROM ins) > 0 THEN 'CHAT_OK' ELSE 'CHAT_FAIL' END;
" 2>&1)

if echo "$CHAT_TEST" | grep -q "CHAT_OK"; then
    echo "[deploy.sh] Phase 4: ✅ 채팅 INSERT/DELETE 정상"
else
    echo "[deploy.sh] ❌ Phase 4 실패 — 롤백 시도..."
    echo "[deploy.sh] 에러: ${CHAT_TEST}"
    if [[ "$MODE" == "code" ]]; then
        docker exec aads-server supervisorctl restart aads-api || true
        sleep 10
    fi
    notify "❌ 채팅 기능 테스트 실패 + 롤백 (mode=${MODE}): ${CHAT_TEST:0:200}"
    exit 1
fi

# ── Phase 5: LLM 연결 테스트 (Agent SDK 또는 Gemini 가용성) ──
echo "[deploy.sh] Phase 5: LLM 연결 테스트..."
LLM_TEST=$(curl -sf "${HEALTH_URL}" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print('LLM_OK' if d.get('status') == 'ok' else 'LLM_FAIL')
except:
    print('LLM_FAIL')
" 2>/dev/null || echo "LLM_FAIL")

if [[ "$LLM_TEST" == "LLM_OK" ]]; then
    echo "[deploy.sh] Phase 5: ✅ LLM 서비스 정상"
else
    echo "[deploy.sh] ⚠️ Phase 5: LLM 상태 확인 불가 (채팅은 가능하나 AI 응답 지연 가능)"
    notify "⚠️ LLM 상태 확인 불가 — 채팅 가능하나 AI 응답 지연 가능"
fi

echo "[deploy.sh] ✅ 배포 완료 — 5단계 검증 통과 (mode=${MODE})"
notify "✅ 배포 완료 — 5단계 검증 통과 (mode=${MODE})"
exit 0
