#!/bin/bash
# AADS Pre-Deploy 검증 — 볼륨마운트 코드 변경 후 자동 실행
# [2026-04-02] 신규 작성 — 문법 오류 서비스 중단 방지
# cron: */5 * * * * /root/aads/aads-server/scripts/pre_deploy_validate.sh
#
# 검증 항목:
#   1. Python 파일 문법 검증 (py_compile)
#   2. 주요 import 검증
#   3. 변경 파일 감지 (최근 5분 내 수정된 .py 파일)

COMPOSE_DIR="/root/aads/aads-server"
APP_DIR="/root/aads/aads-server/app"
STATE_FILE="/tmp/aads_last_validate_hash"
LOG="/var/log/aads_pre_deploy.log"

TELEGRAM_BOT_TOKEN=$(grep -oP '^TELEGRAM_BOT_TOKEN=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
TELEGRAM_CHAT_ID=$(grep -oP '^TELEGRAM_CHAT_ID=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)

notify() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -sf -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_CHAT_ID}" \
            -d text="🔍 [AADS Validate] $1" >/dev/null 2>&1 || true
    fi
}

# 최근 5분 내 수정된 .py 파일 목록
CHANGED=$(find "$APP_DIR" -name "*.py" -mmin -5 -type f 2>/dev/null)
if [ -z "$CHANGED" ]; then
    exit 0  # 변경 없으면 스킵
fi

# 변경 파일 해시로 중복 실행 방지
HASH=$(echo "$CHANGED" | md5sum | cut -d' ' -f1)
if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE")" = "$HASH" ]; then
    exit 0
fi
echo "$HASH" > "$STATE_FILE"

# 1. py_compile 문법 검증
ERRORS=""
while IFS= read -r pyfile; do
    ERR=$(python3 -m py_compile "$pyfile" 2>&1)
    if [ $? -ne 0 ]; then
        ERRORS="${ERRORS}\n${pyfile}: ${ERR}"
    fi
done <<< "$CHANGED"

if [ -n "$ERRORS" ]; then
    notify "❌ 문법 오류 감지 — 배포 위험!\n${ERRORS}"
    exit 1
fi

# 2. 주요 모듈 import 검증 (컨테이너 내부에서)
IMPORT_TEST=$(docker exec aads-server python3 -c "
try:
    from app.main import app
    print('IMPORT_OK')
except Exception as e:
    print(f'IMPORT_FAIL: {e}')
" 2>&1)

if echo "$IMPORT_TEST" | grep -q "IMPORT_FAIL"; then
    notify "❌ import 오류 — ${IMPORT_TEST}"
    exit 1
fi

# 변경 파일 수 로깅
COUNT=$(echo "$CHANGED" | wc -l)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ ${COUNT}개 파일 검증 통과" >> "$LOG"
