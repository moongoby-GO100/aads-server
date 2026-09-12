#!/usr/bin/env bash
# 활성 API 슬롯의 응답성을 감시한다.
#
# 2026-09-12 22:17 에 활성 슬롯(green)이 메모리 한도 3GiB 의 91% 까지 차면서
# GC 스래싱에 빠져 응답을 멈췄다. 그런데 아무 신호도 나지 않았다.
#   - Docker healthcheck 는 계속 "healthy" 였다.
#   - nginx 는 연결을 받아들이되 응답하지 않는 상태를 실패로 보지 않아
#     proxy_read_timeout(600s, SSE 때문에 길다) 까지 기다렸다.
# 결국 회장님이 "새로고침이 안 된다"고 알려주기 전까지 아무도 몰랐다.
#
# 여기서는 "죽었나"가 아니라 "제때 답하나"를 본다. 연속 실패해야 조치하므로
# 순간적인 지연으로 컨테이너를 재시작하지 않는다.
set -uo pipefail

STATE_DIR="/root/aads/aads-server"
TIMEOUT="${SLOT_WATCHDOG_TIMEOUT:-8}"
FAIL_THRESHOLD="${SLOT_WATCHDOG_FAILS:-3}"
MEM_WARN_PCT="${SLOT_WATCHDOG_MEM_WARN:-85}"
AUTO_RESTART="${SLOT_WATCHDOG_AUTO_RESTART:-0}"
FAILFILE="/tmp/.aads-slot-watchdog-fails"

log() { echo "$(date '+%F %T') $*"; }

port="$(cat "${STATE_DIR}/.active_port" 2>/dev/null || echo 8100)"
container="$(cat "${STATE_DIR}/.active_container" 2>/dev/null || echo aads-server)"
[[ "$port" =~ ^[0-9]+$ ]] || exit 0

code="$(curl -s -o /dev/null -m "$TIMEOUT" -w '%{http_code}' \
        "http://127.0.0.1:${port}/api/v1/health" 2>/dev/null)"
code="${code:-000}"

# 메모리는 실패가 아니어도 미리 알린다 — 오늘 장애의 선행 신호였다.
mem_pct="$(docker stats "$container" --no-stream --format '{{.MemPerc}}' 2>/dev/null | tr -d '%' | cut -d. -f1)"
if [[ "$mem_pct" =~ ^[0-9]+$ ]] && [ "$mem_pct" -ge "$MEM_WARN_PCT" ]; then
    log "⚠️ ${container} 메모리 ${mem_pct}% (경고선 ${MEM_WARN_PCT}%) — 응답 정지 선행 신호"
fi

if [ "$code" = "200" ]; then
    [ -f "$FAILFILE" ] && { log "${container}:${port} 회복 (연속실패 초기화)"; rm -f "$FAILFILE"; }
    exit 0
fi

fails=$(( $(cat "$FAILFILE" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$fails" > "$FAILFILE"
log "❌ 활성 슬롯 ${container}:${port} 응답 실패 code=${code} 연속=${fails}/${FAIL_THRESHOLD} mem=${mem_pct:-?}%"

[ "$fails" -lt "$FAIL_THRESHOLD" ] && exit 0

# 대기 슬롯이 살아 있는지 확인해둔다. 폴백이 가능한지가 판단의 전제다.
standby_port=$([ "$port" = "8100" ] && echo 8102 || echo 8100)
standby="$(curl -s -o /dev/null -m 5 -w '%{http_code}' \
           "http://127.0.0.1:${standby_port}/api/v1/health" 2>/dev/null)"
standby="${standby:-000}"
log "  대기 슬롯 :${standby_port} = ${standby}"

ALERT="/root/aads/aads-server/scripts/send_disk_alert.sh"
[ -x "$ALERT" ] && "$ALERT" "활성 API ${container}:${port} 무응답(${fails}회, mem=${mem_pct:-?}%). 대기 :${standby_port}=${standby}" >/dev/null 2>&1 || true

if [ "$AUTO_RESTART" = "1" ] && [ "$standby" = "200" ]; then
    log "  대기 슬롯 정상 → ${container} 재시작"
    docker restart "$container" >/dev/null 2>&1 && log "  재시작 완료" || log "  재시작 실패"
    rm -f "$FAILFILE"
else
    log "  자동 재시작 꺼짐(SLOT_WATCHDOG_AUTO_RESTART=1 로 활성화) — 수동 조치 필요"
fi
exit 0
