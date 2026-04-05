#!/bin/bash
# AADS Hot-Reload — API 기반 무중단 재시작 (0ms 다운타임)
# [2026-04-05] 생성 — 코드 변경 시 무중단 모듈 재로드 표준 방법
# 사용: bash /root/aads/aads-server/scripts/reload-api.sh
# 또는 컨테이너 내부: bash /app/scripts/reload-api.sh

set -e

LOG="/var/log/blue_green_deploy.log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] $1" | tee -a "$LOG"; }

# 호스트에서 실행 시 localhost:8100, 컨테이너 내에서는 localhost:8080
INTERNAL_URL="http://localhost:8080"
if [ ! -f "/tmp/gunicorn.pid" ]; then
    # 컨테이너 외부 호스트에서 실행 중
    INTERNAL_URL="http://127.0.0.1:8100"
fi

log "[START] Hot-Reload 시작 — POST $INTERNAL_URL/api/v1/ops/hot-reload"

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
