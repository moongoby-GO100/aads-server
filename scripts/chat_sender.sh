#!/bin/bash
# AADS Chat Sender — 보고서를 매니저 채팅으로 전송 + STATUS.md chat_delivered 갱신
# AADS-147: 브라우저 자동화 실패 시 복구 경로 지원
#
# 사용:
#   ./chat_sender.sh <task_id> <report_url> [message]
#   ./chat_sender.sh AADS-147 "https://github.com/.../reports/RESULT.md" "작업 완료 보고"
#
# 동작:
#   1) Telegram으로 보고서 링크 전송
#   2) 전송 성공 시 aads-docs/STATUS.md chat_delivered → true 갱신
#   3) git add + commit + push (aads-docs)

set -euo pipefail

TASK_ID="${1:-}"
REPORT_URL="${2:-}"
MESSAGE="${3:-}"
STATUS_FILE="/root/aads/aads-docs/STATUS.md"
DOCS_DIR="/root/aads/aads-docs"

# ─── 인자 검증 ───────────────────────────────────────────────
if [ -z "$TASK_ID" ]; then
    echo "사용법: $0 <task_id> [report_url] [message]"
    exit 1
fi

NOW=$(TZ='Asia/Seoul' date '+%Y-%m-%d %H:%M KST')

# ─── 보고서 내용 조회 ─────────────────────────────────────────
if [ -n "$REPORT_URL" ]; then
    REPORT_CONTENT=$(curl -s --max-time 10 "$REPORT_URL" 2>/dev/null | head -c 1500 || echo "")
else
    REPORT_CONTENT=""
fi

# ─── 전송 메시지 구성 ─────────────────────────────────────────
if [ -z "$MESSAGE" ]; then
    MESSAGE="✅ [${TASK_ID}] 작업 완료 보고
━━━━━━━━━━━━━
📅 ${NOW}
📋 ${REPORT_URL:-N/A}
━━━━━━━━━━━━━
${REPORT_CONTENT:+보고서 요약:
${REPORT_CONTENT:0:500}}"
fi

# ─── Telegram 전송 ────────────────────────────────────────────
source /root/.genspark/.env 2>/dev/null || true

TOKEN="${TELEGRAM_BOT_TOKEN:-$GO100_TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-$GO100_TELEGRAM_CHAT_ID:-}"

SEND_RESULT=0
if [ -n "$TOKEN" ] && [ -n "$CHAT_ID" ]; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        --max-time 15 \
        -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}&text=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$MESSAGE" 2>/dev/null || echo "${MESSAGE// /%20}")&parse_mode=Markdown" \
        2>/dev/null)
    if [ "$HTTP_CODE" = "200" ]; then
        echo "[CHAT-SENDER] Telegram 전송 성공: task=${TASK_ID} HTTP=${HTTP_CODE}"
        SEND_RESULT=0
    else
        echo "[CHAT-SENDER] Telegram 전송 실패: task=${TASK_ID} HTTP=${HTTP_CODE}"
        SEND_RESULT=1
    fi
else
    echo "[CHAT-SENDER] Telegram 토큰 없음 — 전송 스킵 (TOKEN/CHAT_ID 미설정)"
    SEND_RESULT=1
fi

# ─── AADS API를 통한 채널 알림 시도 ──────────────────────────
AADS_API="https://aads.newtalk.kr/api/v1"
MONITOR_KEY=""
if [ -f /root/.env.aads ]; then
    MONITOR_KEY=$(grep "^AADS_MONITOR_KEY=" /root/.env.aads 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')
fi

if [ -n "$MONITOR_KEY" ]; then
    API_RESULT=$(curl -s -o /dev/null -w "%{http_code}" \
        --max-time 10 \
        -X POST "${AADS_API}/ops/chat-notify" \
        -H "Content-Type: application/json" \
        -H "X-Monitor-Key: ${MONITOR_KEY}" \
        -d "{\"task_id\":\"${TASK_ID}\",\"report_url\":\"${REPORT_URL}\",\"message\":\"작업 완료\"}" \
        2>/dev/null || echo "000")
    if [ "$API_RESULT" = "200" ] || [ "$API_RESULT" = "201" ]; then
        echo "[CHAT-SENDER] AADS API 알림 성공: HTTP=${API_RESULT}"
        SEND_RESULT=0
    fi
fi

# ─── 전송 성공 시 STATUS.md chat_delivered → true ──────────────
if [ "$SEND_RESULT" -eq 0 ]; then
    if [ -f "$STATUS_FILE" ]; then
        # chat_delivered: false → true
        sed -i 's/^chat_delivered: false$/chat_delivered: true/' "$STATUS_FILE"
        echo "[CHAT-SENDER] STATUS.md chat_delivered=true 갱신"

        # git push
        if [ -d "${DOCS_DIR}/.git" ]; then
            git -C "$DOCS_DIR" add STATUS.md 2>&1
            if git -C "$DOCS_DIR" diff --cached --quiet 2>/dev/null; then
                echo "[CHAT-SENDER] STATUS.md 변경 없음 (이미 true)"
            else
                git -C "$DOCS_DIR" commit -m "chore(status): ${TASK_ID} chat_delivered=true ($(date '+%Y-%m-%d %H:%M KST'))" 2>&1
                git -C "$DOCS_DIR" push origin main 2>&1 || true
                echo "[CHAT-SENDER] STATUS.md git push 완료"
            fi
        fi
    else
        echo "[CHAT-SENDER] WARNING: STATUS.md 없음 — ${STATUS_FILE}"
    fi
else
    echo "[CHAT-SENDER] 전송 실패 — STATUS.md chat_delivered 유지 (false)"
fi

echo "[CHAT-SENDER] 완료: task=${TASK_ID} send_result=${SEND_RESULT}"
exit $SEND_RESULT
