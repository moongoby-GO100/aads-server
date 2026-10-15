#!/usr/bin/env bash
# 공통 텔레그램 알림 — 호스트 크론 스크립트(token_keeper, disk_guard, disk_cleanup_v2,
# active_slot_watchdog)가 모두 이 하나를 부른다.
#
# 2026-10-15: 위 스크립트들은 존재하지 않는 옛 알림 스크립트를
# `[ -x "$ALERT" ] && ... || true` 로 불러 파일이 없으면 조용히 건너뛰었다.
# 슬롯1 OAuth 갱신이 10/14 18:00 KST 부터 거부됐는데 알림이 한 번도 나가지 않은 이유다.
#
# 사용: aads_notify.sh "메시지"
# 종료코드: 0 전송 성공 / 1 전송 실패 / 2 자격증명 없음 / 64 사용법 오류
# 실패는 조용히 넘기지 않는다 — stderr 와 로그 파일에 ERROR 한 줄을 남긴다.
#
# 자격증명(R-KEY): 환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, 없으면 .env 에서만 읽는다.
# .env 를 source 하지 않고 필요한 두 줄만 뽑는다(값에 공백·특수문자가 있어 source 하면 죽는다).
set -uo pipefail

NOTIFY_LOG="${AADS_NOTIFY_LOG:-/var/log/aads-notify.log}"
ENV_FILES="${AADS_NOTIFY_ENV_FILES:-/root/aads/aads-server/.env:/root/aads/.env}"
HOST_TAG="$(hostname -s 2>/dev/null || echo host)"

_ts() { date '+%F %T'; }

_error() {
    local line
    line="$(_ts) ERROR aads_notify: $*"
    echo "$line" >&2
    { echo "$line" >>"$NOTIFY_LOG"; } 2>/dev/null || true
}

if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
    _error "사용법: aads_notify.sh \"메시지\""
    exit 64
fi
MESSAGE="$1"

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT="${TELEGRAM_CHAT_ID:-}"
if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
    IFS=':' read -r -a _files <<<"$ENV_FILES"
    for f in "${_files[@]}"; do
        [ -f "$f" ] || continue
        while IFS= read -r line; do
            case "$line" in
                TELEGRAM_BOT_TOKEN=*) key=T; val="${line#TELEGRAM_BOT_TOKEN=}" ;;
                TELEGRAM_CHAT_ID=*)   key=C; val="${line#TELEGRAM_CHAT_ID=}" ;;
                *) continue ;;
            esac
            val="${val%$'\r'}"; val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
            [ -n "$val" ] || continue
            if [ "$key" = T ] && [ -z "$TOKEN" ]; then TOKEN="$val"; fi
            if [ "$key" = C ] && [ -z "$CHAT" ]; then CHAT="$val"; fi
        done <"$f"
    done
fi

if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
    _error "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 없음(env, ${ENV_FILES}) — 알림 미발송: ${MESSAGE}"
    exit 2
fi

# 토큰을 URL 에 넣으면 프로세스 목록에 노출되므로 curl 설정은 stdin 으로 넘긴다.
resp="$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TOKEN" \
    | curl -sS -m 20 -K - \
        --data-urlencode "chat_id=${CHAT}" \
        --data-urlencode "text=[${HOST_TAG}] ${MESSAGE}" 2>&1)"
rc=$?
if [ "$rc" -ne 0 ] || ! printf '%s' "$resp" | grep -q '"ok":[[:space:]]*true'; then
    # 응답에 토큰이 에코될 일은 없지만, 만에 하나를 위해 지운다.
    safe="$(printf '%s' "$resp" | tr -d '\n' | sed "s|${TOKEN}|***|g" | cut -c1-200)"
    _error "텔레그램 전송 실패(curl rc=${rc}): ${safe} — 알림 미발송: ${MESSAGE}"
    exit 1
fi
{ echo "$(_ts) OK aads_notify: ${MESSAGE}" >>"$NOTIFY_LOG"; } 2>/dev/null || true
exit 0
