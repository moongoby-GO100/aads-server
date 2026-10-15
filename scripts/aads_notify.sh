#!/usr/bin/env bash
# 공통 알림 — 호스트 크론 스크립트(token_keeper, disk_guard, disk_cleanup_v2,
# active_slot_watchdog)가 모두 이 하나를 부른다.
#
# 채널(AADS_NOTIFY_CHANNELS, 기본 "inapp,telegram") — 순서대로 시도하고 첫 성공에서 멈춘다.
#   inapp    앱 내 알림함. 컨테이너 안에서 app.services.ohvis_alert.notify() 로 alert_history 에 넣는다.
#            대표님이 대시보드(/projects/dashboard/alerts, /inbox 알림 탭)에서 보는 그 저장소다.
#            새 테이블은 만들지 않는다. 컨테이너는 keeper 와 같은 방식(.active_container)으로 고른다.
#   telegram 보조. 호스트 /etc/hosts 가 api.telegram.org 를 의도적으로 0.0.0.0 으로 막아 둔
#            (AADS_TELEGRAM_BLOCK_20260605, 알림 폭주 방지) 동안에는 실패한다 — 차단은 풀지 않는다.
#
# 2026-10-15: 텔레그램 단일 채널이던 시절 14:12 테스트 발송이 curl rc=60 으로 실패해
# 알림이 아무에게도 닿지 않았다. 그 전에는 존재하지 않는 옛 스크립트를
# `[ -x ] && ... || true` 로 불러 조용히 건너뛰었다(슬롯1 갱신 거부 10/14 18:00~).
#
# 사용: aads_notify.sh "메시지"
# 종료코드: 0 한 채널이라도 성공(또는 반복 억제로 건너뜀) / 1 모든 채널 실패 / 64 사용법 오류
# 실패는 조용히 넘기지 않는다 — 채널별로 stderr 와 로그 파일에 ERROR 한 줄을 남긴다.
#
# 반복 억제: 같은 메시지(또는 AADS_NOTIFY_KEY)는 AADS_NOTIFY_DEDUPE_MIN(기본 60)분 안에
# 1회만 보낸다. 성공한 발송만 기록하므로 실패한 알림은 다음 호출에서 다시 시도한다.
#
# 자격증명(R-KEY): 텔레그램은 환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, 없으면 .env 에서만 읽는다.
# .env 를 source 하지 않고 필요한 두 줄만 뽑는다(값에 공백·특수문자가 있어 source 하면 죽는다).
# 앱 내 채널은 시크릿이 필요 없다(컨테이너가 이미 DB 접속 정보를 갖는다).
set -uo pipefail

NOTIFY_LOG="${AADS_NOTIFY_LOG:-/var/log/aads-notify.log}"
ENV_FILES="${AADS_NOTIFY_ENV_FILES:-/root/aads/aads-server/.env:/root/aads/.env}"
CHANNELS="${AADS_NOTIFY_CHANNELS:-inapp,telegram}"
STATE_DIR="${AADS_NOTIFY_STATE_DIR:-/var/lib/aads-notify}"
DEDUPE_MIN="${AADS_NOTIFY_DEDUPE_MIN:-60}"
ACTIVE_CONTAINER_FILE="${AADS_NOTIFY_ACTIVE_CONTAINER_FILE:-/root/aads/aads-server/.active_container}"
HOST_TAG="$(hostname -s 2>/dev/null || echo host)"

_ts() { date '+%F %T'; }

_error() {
    local line
    line="$(_ts) ERROR aads_notify: $*"
    echo "$line" >&2
    { echo "$line" >>"$NOTIFY_LOG"; } 2>/dev/null || true
}

_info() { { echo "$(_ts) $*" >>"$NOTIFY_LOG"; } 2>/dev/null || true; }

if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
    _error "사용법: aads_notify.sh \"메시지\""
    exit 64
fi
MESSAGE="$1"

# ── 반복 억제 ────────────────────────────────────────────────────────
DEDUPE_FILE=""
if [[ "$DEDUPE_MIN" =~ ^[0-9]+$ ]] && [ "$DEDUPE_MIN" -gt 0 ]; then
    if mkdir -p "$STATE_DIR" 2>/dev/null; then
        DEDUPE_FILE="${STATE_DIR}/$(printf '%s' "${AADS_NOTIFY_KEY:-$MESSAGE}" | sha256sum | cut -c1-32)"
        find "$STATE_DIR" -maxdepth 1 -type f -mmin +1440 -delete 2>/dev/null || true
        if [ -f "$DEDUPE_FILE" ] && [ -z "$(find "$DEDUPE_FILE" -mmin +"$DEDUPE_MIN" 2>/dev/null)" ]; then
            _info "SUPPRESSED aads_notify: ${DEDUPE_MIN}분 안에 같은 알림을 이미 보냄 — ${MESSAGE}"
            exit 0
        fi
    else
        _error "반복 억제 상태 디렉터리 생성 실패(${STATE_DIR}) — 억제 없이 발송한다"
    fi
fi

# ── 채널: 앱 내 알림함 ────────────────────────────────────────────────
_infer_severity() {
    case "$MESSAGE" in
        *테스트*) echo info ;;
        *🔴*|*⛔*|*CRITICAL*) echo critical ;;
        *✅*) echo info ;;
        *) echo warning ;;
    esac
}

_INAPP_PY='
import asyncio, os, sys
sys.path.insert(0, "/app")
from app.core.db_pool import init_pool
from app.services import ohvis_alert

async def main():
    await init_pool()
    ok = await ohvis_alert.notify(
        os.environ["AADS_NOTIFY_TITLE"],
        os.environ["AADS_NOTIFY_BODY"],
        severity=os.environ["AADS_NOTIFY_SEVERITY"],
        category="host_notify",
        project="AADS",
        server=os.environ.get("AADS_NOTIFY_HOST", "")[:20] or None,
        dedupe_minutes=0,
    )
    print("AADS_NOTIFY_RESULT=" + ("ok" if ok else "fail"))

asyncio.run(main())
'

send_inapp() {
    local container first out rc
    container="${AADS_NOTIFY_CONTAINER:-$(cat "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true)}"
    container="${container:-aads-server}"
    first="${MESSAGE%%$'\n'*}"
    # 메시지는 비밀이 아니지만 argv 에 길게 싣지 않도록 환경변수(이름만 넘김)로 전달한다.
    out="$(
        export AADS_NOTIFY_TITLE="[${HOST_TAG}] ${first:0:200}" AADS_NOTIFY_BODY="$MESSAGE" \
               AADS_NOTIFY_SEVERITY="${AADS_NOTIFY_SEVERITY:-$(_infer_severity)}" AADS_NOTIFY_HOST="$HOST_TAG"
        printf '%s' "$_INAPP_PY" \
            | timeout 60 docker exec -i -e AADS_NOTIFY_TITLE -e AADS_NOTIFY_BODY \
                -e AADS_NOTIFY_SEVERITY -e AADS_NOTIFY_HOST "$container" python3 - 2>&1
    )"
    rc=$?
    if [ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q '^AADS_NOTIFY_RESULT=ok$'; then
        return 0
    fi
    _error "앱 내 알림함 저장 실패(container=${container}, rc=${rc}): $(printf '%s' "$out" | tail -1 | tr -d '\n' | cut -c1-200) — 알림 미발송: ${MESSAGE}"
    return 1
}

# ── 채널: 텔레그램(보조) ──────────────────────────────────────────────
send_telegram() {
    local TOKEN CHAT f line key val resp rc safe
    TOKEN="${TELEGRAM_BOT_TOKEN:-}"
    CHAT="${TELEGRAM_CHAT_ID:-}"
    if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
        local -a _files
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
        _error "텔레그램 자격증명 없음(TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID, env, ${ENV_FILES}) — 알림 미발송: ${MESSAGE}"
        return 1
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
        return 1
    fi
    return 0
}

# ── 발송: 첫 성공에서 멈춘다 ──────────────────────────────────────────
delivered=""
IFS=',' read -r -a _chs <<<"$CHANNELS"
for ch in "${_chs[@]}"; do
    case "$ch" in
        inapp)    send_inapp && { delivered="inapp"; break; } ;;
        telegram) send_telegram && { delivered="telegram"; break; } ;;
        "") ;;
        *) _error "알 수 없는 채널: ${ch}" ;;
    esac
done

if [ -z "$delivered" ]; then
    _error "모든 채널 실패(${CHANNELS}) — 알림 미발송: ${MESSAGE}"
    exit 1
fi

[ -z "$DEDUPE_FILE" ] || { : >"$DEDUPE_FILE"; } 2>/dev/null || true
_info "OK aads_notify[${delivered}]: ${MESSAGE}"
exit 0
