#!/bin/bash
# GitHub 디바이스 로그인 — 멱등 발급기.
#
# 사용:
#   gh_device_login.sh            # 살아있는 코드가 있으면 그것을 다시 보여준다
#   gh_device_login.sh --force    # 기존 폴러를 정리하고 새 코드를 발급한다
#   gh_device_login.sh --status   # 발급하지 않고 현재 상태만 본다
#
# 왜 필요한가. 2026-09-16, 같은 작업에서 사용자에게 안내한 user_code 가 네 번
# 바뀌었다(6096-7B00 → 0F64-4E3B → 49DE-17F4 → EF6D-8057). 원인은 두 가지다.
#
#   1) 턴 재실행. 에이전트 응답 턴이 재실행되면서 device code 발급 명령이 다시
#      돌았다. 그때마다 기존 폴러가 죽고 새 코드가 나왔으므로, 사용자 화면에
#      떠 있던 코드는 "아무도 폴링하지 않는 고아 코드" 가 됐다. 사용자가
#      승인해도 토큰을 회수할 주체가 없다 — 승인이 통째로 버려진다.
#   2) 중복 폴러. 발급을 반복하면 폴러가 여러 세트 쌓여 같은 엔드포인트를
#      5초보다 빠르게 두드린다. GitHub 이 slow_down 을 돌려주고, 그 구간에
#      떨어진 승인은 회수되지 않는다.
#
# 그래서 발급 자체를 멱등으로 만든다. 살아있는 폴러 + 만료 전 코드가 있으면
# 새로 발급하지 않고 있는 코드를 그대로 출력한다. 재실행돼도 사용자 화면의
# 코드는 바뀌지 않는다.
#
# 규칙(R-BG): 폴러는 timeout 으로 시간 상한을 걸고, 한 번에 한 세트만 띄운다.

set -u
umask 077

CLIENT_ID="178c6fc778ccc68e1d6a"   # GitHub CLI 공개 OAuth 앱 (비밀값 아님)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLLER="${HERE}/gh_token_to_env.sh"
ENV_FILE="/root/aads/aads-server/.env"
MAP="/tmp/gh_device_map.txt"
LOG="/tmp/gh_token_to_env.log"
LOCK="/tmp/gh_device_login.lock"
POLLER_PAT='scripts/gh_token_to_en[v].sh'

MODE="${1:-}"

# 동시 실행 차단. 재실행이 겹쳐도 발급은 한 번만 일어난다.
exec 9>"$LOCK"
if ! flock -w 10 9; then
    echo "다른 발급이 진행 중이다 — 중단" >&2
    exit 3
fi

live_pollers() { pgrep -f "$POLLER_PAT" 2>/dev/null; }

map_get() { grep -m1 "^$1=" "$MAP" 2>/dev/null | cut -d= -f2-; }

# 살아있는 세션인가: 폴러가 떠 있고, 매핑의 device_code 를 그 폴러가 잡고 있고,
# 아직 만료 전인가. 셋 다 맞아야 "있는 코드를 다시 보여준다".
session_alive() {
    [ -f "$MAP" ] || return 1
    local dc exp now pid cmd
    dc="$(map_get device_code)"
    exp="$(map_get expires_epoch)"
    [ -n "$dc" ] && [ -n "$exp" ] || return 1
    now="$(date +%s)"
    [ "$now" -lt "$exp" ] || return 1
    for pid in $(live_pollers); do
        cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)"
        case "$cmd" in *"$dc"*) return 0 ;; esac
    done
    return 1
}

show() {
    local uc exp
    uc="$(map_get user_code)"
    exp="$(map_get expires)"
    echo "user_code=${uc}"
    echo "url=https://github.com/login/device"
    echo "expires=${exp} KST"
    echo "pollers=$(live_pollers | wc -l)"
    if grep -q '^GH_TOKEN=' "$ENV_FILE" 2>/dev/null; then
        echo "gh_token=등록됨"
    else
        echo "gh_token=미등록(승인 대기)"
    fi
}

# 이미 등록돼 있으면 발급할 이유가 없다.
if [ "$MODE" != "--force" ] && grep -q '^GH_TOKEN=' "$ENV_FILE" 2>/dev/null; then
    echo "state=already_registered"
    show
    exit 0
fi

if [ "$MODE" = "--status" ]; then
    if session_alive; then echo "state=alive"; else echo "state=none"; fi
    show
    exit 0
fi

if [ "$MODE" != "--force" ] && session_alive; then
    # 재실행 방어 지점. 새로 뽑지 않는다.
    echo "state=reuse"
    show
    exit 0
fi

# 여기부터 신규 발급. 기존 폴러는 전부 정리한다(중복 폴링 → slow_down 방지).
for pid in $(live_pollers); do kill "$pid" 2>/dev/null; done
sleep 1

RESP="$(curl -s -X POST https://github.com/login/device/code \
    -H "Accept: application/json" \
    -d "client_id=${CLIENT_ID}" -d "scope=" --max-time 15)"

DC="$(printf '%s' "$RESP" | grep -o '"device_code":"[^"]*"' | cut -d'"' -f4)"
UC="$(printf '%s' "$RESP" | grep -o '"user_code":"[^"]*"' | cut -d'"' -f4)"
EX="$(printf '%s' "$RESP" | grep -o '"expires_in":[0-9]*' | cut -d: -f2)"

if [ -z "$DC" ] || [ -z "$UC" ]; then
    echo "발급 실패 — GitHub 응답에 device_code/user_code 가 없다" >&2
    exit 1
fi

EX="${EX:-900}"
EXP_EPOCH="$(( $(date +%s) + EX ))"

{
    echo "user_code=${UC}"
    echo "device_code=${DC}"
    echo "issued=$(date '+%H:%M:%S')"
    echo "expires=$(date -d "@${EXP_EPOCH}" '+%H:%M:%S')"
    echo "expires_epoch=${EXP_EPOCH}"
} > "$MAP"
chmod 600 "$MAP"

: > "$LOG"
setsid nohup timeout "$EX" bash "$POLLER" "$DC" >/dev/null 2>&1 &
sleep 1

echo "state=issued"
show
exit 0
