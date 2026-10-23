#!/bin/bash
set -euo pipefail

# Host CLI credential isolation.
#
# 갱신 소유권과 잠금의 수명, 그리고 호출의 수명을 모두 분리한다.
#   A. prepare — 만료 임박 시에만 슬롯 잠금(배타)을 잡고, 소유자 단 하나가 권위 자격증명을
#                다시 읽어 CLI 로 갱신 → 검증 → 0600 원자 저장한다. 잠금은 이 구간에만 쥔다.
#   B. call    — 잠금 없이 비공개 HOME 에서 모델을 호출한다. 비공개 사본에는 refreshToken 이
#                없으므로 CLI 가 같은 grant 를 다시 소진할 수 없다(CLI 2.1.280 소스로 확인).
#   C. watch   — 호출 중 만료가 다가오면 helper 가(잠금 아래, 유일한 소유자로) 갱신하고
#                새 accessToken 만 비공개 사본에 밀어 넣는다.
#   D. reap    — 종료·취소·타임아웃 시 이 호출의 태그(AADS_CLAUDE_CALL_ID)가 붙은 프로세스와
#                그 자손만 TERM → KILL 로 회수한다. 태그 없는 프로세스는 건드리지 않는다.
# 판정·저장·회수는 모두 claude_slot_credential.py 가 한다(도커 래퍼와 동일 구현).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CREDENTIAL_FILE="${CLAUDE_SLOT_CREDENTIALS_FILE:?missing slot credential file}"
LOCK_FILE="${CLAUDE_SLOT_CREDENTIAL_LOCK_FILE:-$(dirname -- "$CREDENTIAL_FILE")/.credentials.lock}"
SLOT="${CLAUDE_OAUTH_SLOT:-unknown}"
REFRESH_LOCK_WINDOW_SEC="${CLAUDE_SLOT_REFRESH_LOCK_WINDOW_SEC:-600}"
REFRESH_LOCK_WAIT_SEC="${CLAUDE_SLOT_REFRESH_LOCK_WAIT_SEC:-30}"
REFRESH_TIMEOUT_SEC="${CLAUDE_SLOT_REFRESH_TIMEOUT_SEC:-40}"
REFRESH_RETRY_SEC="${CLAUDE_SLOT_REFRESH_RETRY_SEC:-60}"
WATCH_BELOW_SEC="${CLAUDE_SLOT_WATCH_BELOW_SEC:-6000}"
WATCH_INTERVAL_SEC="${CLAUDE_SLOT_WATCH_INTERVAL_SEC:-5}"
WATCH_STOP_WAIT_SEC="${CLAUDE_SLOT_WATCH_STOP_WAIT_SEC:-15}"
REAP_GRACE_SEC="${CLAUDE_SLOT_REAP_GRACE_SEC:-5}"
REAP_KILL_WAIT_SEC="${CLAUDE_SLOT_REAP_KILL_WAIT_SEC:-3}"
CREDENTIAL_HELPER="${CLAUDE_SLOT_CREDENTIAL_HELPER:-${SCRIPT_DIR}/claude_slot_credential.py}"
TEMP_HOME=""
CHILD_PID=""
CALL_ID=""
WATCH_PID=""
STATE_DIR=""
SIGNAL_EXIT=""

helper() {
    python3 "$CREDENTIAL_HELPER" "$@"
}

# watcher 는 함수(helper)가 아니라 python3 를 직접 백그라운드로 띄운다 — 함수를 &로 띄우면
# WATCH_PID 가 서브셸이 되어 TERM 이 python 에 닿지 않는다.
# 갱신 도중인 watcher 를 바로 KILL 하면 이미 소진한 grant 를 잃는다. TERM 을 주고 helper 가
# 커밋을 마칠 시간을 상한 안에서 준 뒤에만 KILL 한다.
stop_watcher() {
    [[ -n "$WATCH_PID" ]] || return 0
    kill -TERM "$WATCH_PID" >/dev/null 2>&1
    local waited=0
    while kill -0 "$WATCH_PID" 2>/dev/null && (( waited < WATCH_STOP_WAIT_SEC * 10 )); do
        sleep 0.1
        waited=$((waited + 1))
    done
    kill -KILL "$WATCH_PID" >/dev/null 2>&1
    wait "$WATCH_PID" >/dev/null 2>&1
    WATCH_PID=""
}

# 정리 순서: ① 호출 프로세스 트리 회수(모델·도구 자손 포함) ② 래퍼가 직접 띄운 자식 wait
# ③ watcher 정지 ④ 임시 자격증명·상태 삭제. 모델이 먼저 멈춰야 새 갱신이 일어나지 않는다.
cleanup() {
    local exit_code=$?
    trap - EXIT
    set +e
    if [[ -n "$SIGNAL_EXIT" ]]; then
        exit_code="$SIGNAL_EXIT"
    fi
    if [[ -n "$CALL_ID" ]]; then
        helper reap --call-id "$CALL_ID" --grace "$REAP_GRACE_SEC" --kill-wait "$REAP_KILL_WAIT_SEC" >/dev/null || { [[ "$exit_code" != 0 ]] || exit_code=1; }
    fi
    if [[ -n "$CHILD_PID" ]]; then
        kill -KILL "$CHILD_PID" 2>/dev/null
        wait "$CHILD_PID" 2>/dev/null
    fi
    stop_watcher
    [[ -n "$TEMP_HOME" ]] && rm -rf -- "$TEMP_HOME"
    [[ -n "$STATE_DIR" ]] && rm -rf -- "$STATE_DIR"
    exit "$exit_code"
}
trap cleanup EXIT
trap 'SIGNAL_EXIT=143; exit 143' TERM
trap 'SIGNAL_EXIT=130; exit 130' INT
trap 'SIGNAL_EXIT=129; exit 129' HUP

[[ -f "$CREDENTIAL_FILE" ]] || {
    echo "slot credential file is missing (slot=${SLOT})" >&2
    exit 78
}
command -v python3 >/dev/null 2>&1 || {
    echo "python3 is required for slot credential handling" >&2
    exit 78
}
[[ -f "$CREDENTIAL_HELPER" ]] || {
    echo "slot credential helper is missing: ${CREDENTIAL_HELPER}" >&2
    exit 78
}

mkdir -p -- "$(dirname "$LOCK_FILE")"
STATE_DIR="$(mktemp -d "/tmp/claude-slot-state-${SLOT}.XXXXXX")"
chmod 700 "$STATE_DIR"

prepare_args=(
    prepare --mode host
    --cred "$CREDENTIAL_FILE" --lock "$LOCK_FILE" --slot "$SLOT"
    --state "${STATE_DIR}/state.json" --snapshot "${STATE_DIR}/snapshot.json"
    --window "$REFRESH_LOCK_WINDOW_SEC" --lock-wait "$REFRESH_LOCK_WAIT_SEC"
    --refresh-timeout "$REFRESH_TIMEOUT_SEC" --retry-sec "$REFRESH_RETRY_SEC"
    --watch-below "$WATCH_BELOW_SEC"
    --refresh-bin "${CLAUDE_SLOT_REFRESH_BIN:-${RUNNER_CLAUDE_CLI_BIN:-$(command -v claude || true)}}"
)
if [[ "${CLAUDE_SLOT_FORCE_EXCLUSIVE_LOCK:-0}" == "1" ]]; then
    prepare_args+=(--force)
fi
helper "${prepare_args[@]}" || exit $?

TEMP_HOME="$(mktemp -d "/tmp/claude-slot-${SLOT}.XXXXXX")"
mkdir -p -- "$TEMP_HOME/.claude"
chmod 700 "$TEMP_HOME" "$TEMP_HOME/.claude"
cp -- "${STATE_DIR}/snapshot.json" "$TEMP_HOME/.claude/.credentials.json"
chmod 600 "$TEMP_HOME/.claude/.credentials.json"
[[ -f "$(dirname "$CREDENTIAL_FILE")/settings.json" ]] \
    && cp -- "$(dirname "$CREDENTIAL_FILE")/settings.json" "$TEMP_HOME/.claude/settings.json"

export HOME="$TEMP_HOME"
unset CLAUDE_CODE_OAUTH_TOKEN

python3 "$CREDENTIAL_HELPER" watch --mode host \
    --cred "$CREDENTIAL_FILE" --lock "$LOCK_FILE" \
    --state "${STATE_DIR}/state.json" --slot "$SLOT" \
    --push-path "${TEMP_HOME}/.claude/.credentials.json" \
    --window "$REFRESH_LOCK_WINDOW_SEC" --refresh-timeout "$REFRESH_TIMEOUT_SEC" \
    --retry-sec "$REFRESH_RETRY_SEC" --interval "$WATCH_INTERVAL_SEC" \
    --refresh-bin "${CLAUDE_SLOT_REFRESH_BIN:-${RUNNER_CLAUDE_CLI_BIN:-$(command -v claude || true)}}" \
    >/dev/null &
WATCH_PID=$!

# 백그라운드 + wait: 포그라운드 자식이 돌고 있으면 bash 는 TERM trap 을 자식이 끝난 뒤에야
# 실행한다. 그래서 취소·타임아웃 때 정리가 건너뛰어졌다. stdin 은 3번 fd 로 그대로 넘긴다.
CALL_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
exec 3<&0
env "AADS_CLAUDE_CALL_ID=$CALL_ID" "$@" <&3 3<&- &
CHILD_PID=$!
exec 3<&-
set +e
wait "$CHILD_PID"
status=$?
set -e
CHILD_PID=""
exit "$status"
