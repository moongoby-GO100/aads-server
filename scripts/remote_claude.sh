#!/bin/bash
# remote_claude.sh — T-061: 68서버에서 211서버 Claude Code 직접 호출
# 사용법: ./remote_claude.sh <211서버IP> "<프롬프트>"
# 예시:   ./remote_claude.sh 1.2.3.4 "ShortFlow 상태 보고"
#
# 환경변수:
#   AADS_URL        — AADS API URL (기본: https://aads.newtalk.kr/api/v1)
#   AADS_REMOTE_KEY — Bearer 토큰 (기본: changeme)
#   REMOTE_USER     — SSH 사용자 (기본: root)
#   SSH_KEY         — SSH 키 경로 (선택)

set -euo pipefail

TARGET="${1:-}"
PROMPT="${2:-}"

if [ -z "$TARGET" ] || [ -z "$PROMPT" ]; then
    echo "사용법: $0 <211서버IP> \"<프롬프트>\"" >&2
    exit 1
fi

AADS_URL="${AADS_URL:-https://aads.newtalk.kr/api/v1}"
AADS_REMOTE_KEY="${AADS_REMOTE_KEY:-changeme}"
REMOTE_USER="${REMOTE_USER:-root}"
AGENT_ID="REMOTE_${TARGET//./_}"

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes)
if [ -n "${SSH_KEY:-}" ]; then
    SSH_OPTS+=(-i "$SSH_KEY")
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 대상: ${REMOTE_USER}@${TARGET}" >&2
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 프롬프트: ${PROMPT}" >&2

# ── Claude Code 원격 실행 ────────────────────────────────────────────────────
RESULT=$(ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${TARGET}" \
    "cd /root && claude -p '${PROMPT}' --output-format json 2>/dev/null" 2>/dev/null) || {
    echo "[ERROR] SSH 실행 실패" >&2
    RESULT="{\"error\":\"SSH execution failed\",\"target\":\"${TARGET}\"}"
}

echo "$RESULT"

# ── AADS API로 결과 전송 ─────────────────────────────────────────────────────
TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
PAYLOAD=$(python3 -c "
import json, sys
result_raw = sys.stdin.read()
try:
    content = json.loads(result_raw)
except Exception:
    content = {'raw': result_raw}
payload = {
    'from_agent': '${AGENT_ID}',
    'to_agent': 'AADS_MGR',
    'message_type': 'remote_claude_result',
    'content': content,
    'timestamp': '${TIMESTAMP}',
}
print(json.dumps(payload))
" <<< "$RESULT" 2>/dev/null) || PAYLOAD="{\"from_agent\":\"${AGENT_ID}\",\"to_agent\":\"AADS_MGR\",\"message_type\":\"remote_claude_result\",\"content\":${RESULT},\"timestamp\":\"${TIMESTAMP}\"}"

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${AADS_URL}/memory/cross-message" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${AADS_REMOTE_KEY}" \
    -d "$PAYLOAD" \
    --max-time 15)

echo "[$(date '+%Y-%m-%d %H:%M:%S')] AADS 전송: HTTP ${HTTP_CODE}" >&2
