#!/bin/bash
# AADS Hot-Reload — API 기반 무중단 재시작 (0ms 다운타임)
# [2026-04-05] 생성 — 코드 변경 시 무중단 모듈 재로드 표준 방법
# [2026-04-15] 수정 — 활성 컨테이너 동적 감지 (bluegreen 호환)
# 사용: bash /root/aads/aads-server/scripts/reload-api.sh  (호스트)
# 또는: docker exec <container> bash /app/scripts/reload-api.sh  (컨테이너)

set -e

LOG="/var/log/blue_green_deploy.log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] $1" | tee -a "$LOG" 2>/dev/null || echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] $1"; }

# ── 실행 환경 감지: /.dockerenv 존재 = 컨테이너 내부 ──────────────
if [ -f "/.dockerenv" ]; then
    # 컨테이너 내부: uvicorn 직접 포트 사용
    INTERNAL_URL="http://localhost:8080"
    log "[START] Hot-Reload 시작 (컨테이너 내부) — POST $INTERNAL_URL/api/v1/ops/hot-reload"
else
    # 호스트: 활성 컨테이너 감지 후 docker exec 위임
    COMPOSE_DIR="/root/aads/aads-server"
    ACTIVE_CONTAINER_FILE="${COMPOSE_DIR}/.active_container"
    CONTAINER="aads-server"
    if [ -f "$ACTIVE_CONTAINER_FILE" ]; then
        candidate=$(tr -d '\r\n' < "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true)
        if [ -n "$candidate" ] && docker inspect "$candidate" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
            CONTAINER="$candidate"
        fi
    fi
    log "[INFO] 호스트 실행 → docker exec ${CONTAINER} 위임"
    exec docker exec "$CONTAINER" bash /app/scripts/reload-api.sh
fi

# Hot-Reload API 호출 (모든 Python 모듈 재로드)
RESPONSE=$(curl -sf -X POST \
    "$INTERNAL_URL/api/v1/ops/hot-reload" \
    -H "Content-Type: application/json" \
    -d '{}' \
    2>/dev/null || echo '{"error":"api_unreachable"}')

# 응답 파싱
SUCCESS=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('success', 0))" 2>/dev/null || echo 0)
FAILED=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('failed', 0))" 2>/dev/null || echo 0)

if [ "$FAILED" -gt 0 ]; then
    log "[ERROR] Hot-Reload 실패 (재로드 실패: $FAILED개)"
    exit 1
fi

log "[OK] Hot-Reload 완료 — 재로드=$SUCCESS개"
