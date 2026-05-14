#!/bin/bash
# Codex CLI 인증 자동 유지 스크립트
# 역할: (1) access_token 만료 감시 (2) 프리웜으로 자동 갱신 (3) 마스터→전서버 동기화
# 크론: */30 * * * * /root/aads/aads-server/scripts/codex_auth_sync.sh
# 마스터 서버: 68 (AADS) — 여기서 갱신 후 211, 114로 배포

set -euo pipefail

LOG="/var/log/codex_auth_sync.log"
AUTH_FILE="/root/.codex/auth.json"
CODEX_BIN="/root/.nvm/versions/node/v20.20.0/bin/codex"
WARN_DAYS=3
TELEGRAM_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

SSH_211="211.188.51.113"
SSH_114="-p 7916 114.207.244.86"
SSH_OPTS="-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no"

[[ -f /root/aads/.env ]] && source /root/aads/.env

mkdir -p "$(dirname "$LOG")"

log() {
    printf '[%s] %s\n' "$(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST')" "$*" >> "$LOG"
}

send_telegram() {
    local msg="$1"
    if [[ -n "$TELEGRAM_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${msg}" \
            -d "parse_mode=HTML" > /dev/null 2>&1 || true
    fi
}

get_token_remaining_days() {
    local auth_file="${1:-$AUTH_FILE}"
    python3 -c "
import json, base64, datetime, sys
try:
    d = json.load(open('${auth_file}'))
    at = d['tokens']['access_token']
    parts = at.split('.')
    pad = parts[1] + '=' * (4 - len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(pad))
    exp = payload.get('exp', 0)
    now = datetime.datetime.utcnow().timestamp()
    remaining = (exp - now) / 86400
    print(f'{remaining:.2f}')
except Exception as e:
    print('-1')
    sys.exit(1)
" 2>/dev/null
}

prewarm_codex() {
    log "PREWARM: codex exec 실행으로 토큰 자동 갱신 시도"
    timeout 60 "$CODEX_BIN" exec --sandbox read-only "echo codex-auth-prewarm-ok" > /dev/null 2>&1
    local rc=$?
    if [[ $rc -eq 0 ]]; then
        log "PREWARM: 성공 — 토큰 자동 갱신됨"
        return 0
    else
        log "PREWARM: 실패 (exit=$rc) — 수동 재인증 필요"
        return 1
    fi
}

sync_to_remote() {
    local label="$1"
    shift
    local ssh_target="$*"

    # 원격 서버에 백업 + 복사
    ssh $SSH_OPTS $ssh_target "cp $AUTH_FILE ${AUTH_FILE}.bak 2>/dev/null; true" 2>/dev/null
    scp $SSH_OPTS "$AUTH_FILE" "${ssh_target##* }:$AUTH_FILE" 2>/dev/null
    ssh $SSH_OPTS $ssh_target "chmod 600 $AUTH_FILE" 2>/dev/null

    if [[ $? -eq 0 ]]; then
        log "SYNC: $label ← 동기화 성공"
    else
        log "SYNC: $label ← 동기화 실패"
        send_telegram "🔴 [Codex Auth] ${label} 동기화 실패 — 수동 확인 필요"
    fi
}

# ── 메인 로직 ──

remaining=$(get_token_remaining_days)

if [[ "$remaining" == "-1" ]]; then
    log "ERROR: auth.json 파싱 실패"
    send_telegram "🔴 [Codex Auth] 68서버 auth.json 파싱 실패"
    exit 1
fi

remaining_int=${remaining%%.*}

log "CHECK: access_token 잔여 ${remaining}일"

# 1. 만료됨 또는 1일 이내 → 프리웜으로 갱신 시도
if (( remaining_int < 1 )); then
    log "ALERT: 토큰 만료 임박 (${remaining}일) — 프리웜 갱신 시도"
    send_telegram "🟡 [Codex Auth] 68서버 토큰 ${remaining}일 남음 — 자동 갱신 시도 중"

    if prewarm_codex; then
        new_remaining=$(get_token_remaining_days)
        log "RENEWED: 갱신 후 잔여 ${new_remaining}일"
        send_telegram "✅ [Codex Auth] 68서버 토큰 자동 갱신 성공 — ${new_remaining}일 남음"
        # 갱신된 토큰을 전서버에 동기화
        sync_to_remote "211서버" "$SSH_211"
        sync_to_remote "114서버" $SSH_114
    else
        send_telegram "🔴 [Codex Auth] 68서버 토큰 자동 갱신 실패 — CEO 수동 인증 필요: codex login --device-auth"
    fi

# 2. 3일 이내 → 경고만
elif (( remaining_int < WARN_DAYS )); then
    log "WARN: 토큰 ${remaining}일 남음 — 3일 이내 만료 예정"
    # 하루 1회만 알림 (중복 방지)
    TODAY=$(date +%Y%m%d)
    WARN_FLAG="/tmp/codex_auth_warn_${TODAY}"
    if [[ ! -f "$WARN_FLAG" ]]; then
        send_telegram "🟡 [Codex Auth] 전서버 Codex 토큰 ${remaining}일 남음 — 만료 전 자동 갱신 예정"
        touch "$WARN_FLAG"
    fi

# 3. 3일 이상 → 정상, 동기화만 확인
else
    # 매일 04:30에만 전서버 동기화 (업데이트 스크립트 직후)
    HOUR=$(date +%H)
    MIN=$(date +%M)
    if [[ "$HOUR" == "04" && "$MIN" -ge 25 && "$MIN" -le 35 ]]; then
        log "DAILY_SYNC: 정기 전서버 동기화"
        sync_to_remote "211서버" "$SSH_211"
        sync_to_remote "114서버" $SSH_114
    fi
fi

exit 0
