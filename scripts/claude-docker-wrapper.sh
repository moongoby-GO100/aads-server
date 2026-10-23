#!/bin/bash
set -euo pipefail

# 갱신 소유권·잠금·호출의 수명을 분리한다 (호스트 래퍼와 같은 구조, 같은 helper).
#   A. prepare — 만료가 임박했을 때만 슬롯 잠금을 잡고 갱신 소유자 하나가 권위 파일을
#                다시 읽어 컨테이너 안 CLI 로 갱신 → 검증 → 0600 원자 저장한다.
#   B. call    — 잠금 없이 호출별 컨테이너 HOME 에서 모델을 호출한다. 컨테이너로 복사되는
#                자격증명에는 refreshToken 이 없어 CLI 가 grant 를 다시 소진할 수 없다.
#   C. watch   — 호출 중 갱신이 필요하면 helper 가 유일한 소유자로 갱신하고 accessToken 만 민다.
#   D. reap    — docker exec 클라이언트 PID 만 죽이면 컨테이너 안 모델·도구 프로세스가
#                남는다(docker daemon 이 별도 프로세스로 실행하기 때문). 그래서 호출별 태그
#                AADS_CLAUDE_CALL_ID 를 `docker exec -e` 로 심고, 컨테이너 안에서 태그가 붙은
#                프로세스와 자손만 TERM → KILL 로 회수한다. 다른 세션·컨테이너 전체는 건드리지 않는다.
# 2026-09-16 실측: 갱신이 필요했던 호출 하나가 모델 호출이 끝날 때까지(6분 48초) 잠금을 쥐어
# 같은 슬롯의 다른 세션 9개와 신규 채팅창이 응답하지 못했다. 잠금 대기 시간을 줄이거나
# 잠금을 없애는 것이 아니라, 잠금을 갱신 구간에만 쥐는 것이 해법이다.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER_NAME="${CLAUDE_DOCKER_CONTAINER:-aads-server}"
CONTAINER_WRAPPER="${CLAUDE_DOCKER_WRAPPER:-/app/scripts/claude-oauth-wrapper.sh}"

LOCAL_MCP_CONFIG=""
CONTAINER_MCP_CONFIG=""
CREDENTIAL_FILE="${CLAUDE_SLOT_CREDENTIALS_FILE:-}"
CREDENTIAL_LOCK_FILE="${CLAUDE_SLOT_CREDENTIAL_LOCK_FILE:-}"
OAUTH_SLOT="${CLAUDE_OAUTH_SLOT:-}"
REFRESH_LOCK_WINDOW_SEC="${CLAUDE_SLOT_REFRESH_LOCK_WINDOW_SEC:-600}"
REFRESH_LOCK_WAIT_SEC="${CLAUDE_SLOT_REFRESH_LOCK_WAIT_SEC:-30}"
REFRESH_TIMEOUT_SEC="${CLAUDE_SLOT_REFRESH_TIMEOUT_SEC:-40}"
REFRESH_RETRY_SEC="${CLAUDE_SLOT_REFRESH_RETRY_SEC:-60}"
WATCH_BELOW_SEC="${CLAUDE_SLOT_WATCH_BELOW_SEC:-6000}"
WATCH_INTERVAL_SEC="${CLAUDE_SLOT_WATCH_INTERVAL_SEC:-5}"
WATCH_STOP_WAIT_SEC="${CLAUDE_SLOT_WATCH_STOP_WAIT_SEC:-15}"
REAP_GRACE_SEC="${CLAUDE_SLOT_REAP_GRACE_SEC:-5}"
REAP_KILL_WAIT_SEC="${CLAUDE_SLOT_REAP_KILL_WAIT_SEC:-3}"
DOCKER_CLEANUP_TIMEOUT_SEC="${CLAUDE_SLOT_DOCKER_CLEANUP_TIMEOUT_SEC:-20}"
CREDENTIAL_HELPER="${CLAUDE_SLOT_CREDENTIAL_HELPER:-${SCRIPT_DIR}/claude_slot_credential.py}"
CONTAINER_CREDENTIAL_HOME=""
# 대화 기록 영속 저장소(컨테이너 안 경로). /root/aads/data/claude-sessions 가
# 여기에 마운트돼 있어 컨테이너 교체를 넘어 살아남는다.
CLAUDE_SESSION_STORE="${CLAUDE_SESSION_STORE:-/tmp/.claude-sdk/.claude}"
STATE_DIR=""
CHILD_PID=""
CALL_ID=""
WATCH_PID=""
SIGNAL_EXIT=""

helper() {
    python3 "$CREDENTIAL_HELPER" "$@"
}

# watcher 는 함수(helper)가 아니라 python3 를 직접 백그라운드로 띄운다 — 함수를 &로 띄우면
# WATCH_PID 가 서브셸이 되어 TERM 이 python 에 닿지 않는다.
# 갱신 도중인 watcher 를 바로 KILL 하면 이미 소진한 grant 를 잃는다. TERM 으로 커밋을 마칠
# 시간을 상한 안에서 준 뒤에만 KILL 한다.
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

# 컨테이너 쪽 정리 호출은 모두 상한이 있다(docker 데몬이 응답하지 않아도 래퍼가 걸리지 않는다).
docker_cleanup() {
    timeout -k 2 "$DOCKER_CLEANUP_TIMEOUT_SEC" docker "$@" >/dev/null 2>&1 || true
}

# 정리 순서: ① 컨테이너 안 호출 트리 회수 → 호스트의 docker exec 클라이언트·자손 회수
# (helper reap 이 이 순서로 한다) ② 래퍼가 띄운 자식 wait ③ watcher 정지(갱신 중이면 커밋 대기)
# ④ 컨테이너 임시 HOME·MCP 설정 삭제 ⑤ 호스트 임시 파일 삭제.
cleanup() {
    local exit_code=$?
    trap - EXIT
    set +e
    if [[ -n "$SIGNAL_EXIT" ]]; then
        exit_code="$SIGNAL_EXIT"
    fi
    if [[ -n "$CALL_ID" ]]; then
        helper reap --call-id "$CALL_ID" --container "$CONTAINER_NAME" \
            --grace "$REAP_GRACE_SEC" --kill-wait "$REAP_KILL_WAIT_SEC" \
            --docker-timeout "$DOCKER_CLEANUP_TIMEOUT_SEC" >/dev/null || { [[ "$exit_code" != 0 ]] || exit_code=1; }
    fi
    if [[ -n "$CHILD_PID" ]]; then
        kill -KILL "$CHILD_PID" 2>/dev/null
        wait "$CHILD_PID" 2>/dev/null
    fi
    stop_watcher
    if [[ -n "$CONTAINER_CREDENTIAL_HOME" ]]; then
        docker_cleanup exec "$CONTAINER_NAME" sh -lc "rm -rf '$CONTAINER_CREDENTIAL_HOME'"
    fi
    if [[ -n "$CONTAINER_MCP_CONFIG" ]]; then
        docker_cleanup exec "$CONTAINER_NAME" sh -lc "rm -f '$CONTAINER_MCP_CONFIG'"
    fi
    if [[ -n "$LOCAL_MCP_CONFIG" ]]; then
        rm -f "$LOCAL_MCP_CONFIG" >/dev/null 2>&1 || true
    fi
    [[ -n "$STATE_DIR" ]] && rm -rf -- "$STATE_DIR"
    exit "$exit_code"
}
trap cleanup EXIT
trap 'SIGNAL_EXIT=143; exit 143' TERM
trap 'SIGNAL_EXIT=130; exit 130' INT
trap 'SIGNAL_EXIT=129; exit 129' HUP

args=("$@")

if [[ -n "$CREDENTIAL_FILE" ]]; then
    [[ -f "$CREDENTIAL_FILE" ]] || {
        echo "slot credential file is missing (slot=${OAUTH_SLOT:-unknown})" >&2
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
    CREDENTIAL_LOCK_FILE="${CREDENTIAL_LOCK_FILE:-$(dirname -- "$CREDENTIAL_FILE")/.credentials.lock}"
    mkdir -p -- "$(dirname "$CREDENTIAL_LOCK_FILE")"
    STATE_DIR="$(mktemp -d "/tmp/claude-slot-state-${OAUTH_SLOT:-unknown}.XXXXXX")"
    chmod 700 "$STATE_DIR"

    # 갱신이 필요할 때만 잠금을 잡는다. 대기에는 상한이 있고, 시간 안에 못 잡으면 잠금 없이
    # 실행하지 않고 oauth_slot_busy(75) 로 끝낸다 — 호출자가 잠시 뒤 다시 시도한다.
    prepare_args=(
        prepare --mode docker
        --cred "$CREDENTIAL_FILE" --lock "$CREDENTIAL_LOCK_FILE" --slot "${OAUTH_SLOT:-unknown}"
        --state "${STATE_DIR}/state.json" --snapshot "${STATE_DIR}/snapshot.json"
        --window "$REFRESH_LOCK_WINDOW_SEC" --lock-wait "$REFRESH_LOCK_WAIT_SEC"
        --refresh-timeout "$REFRESH_TIMEOUT_SEC" --retry-sec "$REFRESH_RETRY_SEC"
        --watch-below "$WATCH_BELOW_SEC"
        --container "$CONTAINER_NAME" --container-wrapper "$CONTAINER_WRAPPER"
    )
    if [[ "${CLAUDE_SLOT_FORCE_EXCLUSIVE_LOCK:-0}" == "1" ]]; then
        prepare_args+=(--force)
    fi
    helper "${prepare_args[@]}" || exit $?

    CONTAINER_CREDENTIAL_HOME="/tmp/.claude-relay-slot-${OAUTH_SLOT:-unknown}-${BASHPID}-${RANDOM}"

    docker exec "$CONTAINER_NAME" sh -lc \
        "umask 077; mkdir -p '$CONTAINER_CREDENTIAL_HOME/.claude'" >/dev/null

    # 대화 기록만 영속 볼륨으로 돌린다.
    #
    # 이 HOME 은 슬롯별 자격증명을 격리하려고 호출마다 새로 만들고 끝나면
    # 지운다(a49eee32). 그런데 CLI 는 대화도 $HOME/.claude/projects 에 쓰기
    # 때문에, 격리와 함께 대화까지 매번 버려졌다. 전날 볼륨을 붙여 배포를
    # 넘어 살아남게 해둔 것(a41445df)이 그대로 무력화됐다 —
    # claude-oauth-wrapper.sh 의 `export HOME="${HOME:-/tmp/.claude-sdk}"` 는
    # 래퍼가 HOME 을 넘기는 순간 기본값을 쓰지 않는다.
    #
    # 결과: --resume 대상 대화가 없어 매 턴 전체 프롬프트를 다시 보냈다.
    # 2026-09-13 실측 6시간 resume 42건 중 18건이 "No conversation found",
    # 프롬프트가 5만 자까지 커져 첫 토큰 전에 스트림이 끊겼다.
    #
    # 지워야 할 것(자격증명)과 남겨야 할 것(대화)을 분리한다. projects 만
    # 볼륨으로 링크하고 .credentials.json 은 임시 HOME 에 그대로 둔다.
    # cleanup 의 rm -rf 는 심볼릭 링크를 따라가지 않으므로 볼륨은 안전하다.
    docker exec "$CONTAINER_NAME" sh -lc \
        "umask 077; mkdir -p '$CLAUDE_SESSION_STORE/projects' \
         && ln -sfn '$CLAUDE_SESSION_STORE/projects' '$CONTAINER_CREDENTIAL_HOME/.claude/projects'" \
        >/dev/null 2>&1 || echo "[claude-docker-wrapper] WARN: 대화 볼륨 연결 실패 — 이번 호출은 resume 불가" >&2

    docker cp "${STATE_DIR}/snapshot.json" \
        "${CONTAINER_NAME}:${CONTAINER_CREDENTIAL_HOME}/.claude/.credentials.json" >/dev/null

    python3 "$CREDENTIAL_HELPER" watch --mode docker \
        --cred "$CREDENTIAL_FILE" --lock "$CREDENTIAL_LOCK_FILE" \
        --state "${STATE_DIR}/state.json" --slot "${OAUTH_SLOT:-unknown}" \
        --container "$CONTAINER_NAME" --container-wrapper "$CONTAINER_WRAPPER" \
        --push-path "${CONTAINER_CREDENTIAL_HOME}/.claude/.credentials.json" \
        --window "$REFRESH_LOCK_WINDOW_SEC" --refresh-timeout "$REFRESH_TIMEOUT_SEC" \
        --retry-sec "$REFRESH_RETRY_SEC" --interval "$WATCH_INTERVAL_SEC" >/dev/null &
    WATCH_PID=$!
fi

for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[$i]}" != "--mcp-config" ]]; then
        continue
    fi
    next_index=$((i + 1))
    if (( next_index >= ${#args[@]} )); then
        break
    fi
    source_config="${args[$next_index]}"
    if [[ ! -f "$source_config" ]]; then
        break
    fi

    LOCAL_MCP_CONFIG="$(mktemp /tmp/claude-mcp.XXXXXX.json)"
    CONTAINER_MCP_CONFIG="/tmp/$(basename "$LOCAL_MCP_CONFIG")"

    python3 - "$source_config" "$LOCAL_MCP_CONFIG" <<'PY'
import json
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
with open(src_path, "r", encoding="utf-8") as src:
    config = json.load(src)

for server in config.get("mcpServers", {}).values():
    args = server.get("args", [])
    # 세션 id 는 두 자리 중 하나에 온다.
    #   - env  : 릴레이가 template 모드로 넣을 때 (대부분의 경로)
    #   - args : cfg 의 command 가 docker 일 때 `-e AADS_SESSION_ID=...`
    # env 를 안 보고 args 만 뒤지던 탓에 거의 언제나 빈 값이 나왔고,
    # 아래에서 `AADS_SESSION_ID=''` 로 **덮어써서** CLI 가 제대로 넘겨준
    # 환경변수까지 지웠다. 그래서 세션에 묶인 도구가 전부 실패했다 —
    # 2026-09-15 #310 주도 세션의 `ask_session` 5/5 `origin_session_missing`.
    session_id = str((server.get("env") or {}).get("AADS_SESSION_ID") or "")
    if not session_id:
        for idx, arg in enumerate(args[:-1]):
            if arg == "-e" and args[idx + 1].startswith("AADS_SESSION_ID="):
                session_id = args[idx + 1].split("=", 1)[1]
                break
    server["command"] = "sh"
    if session_id:
        escaped_session = session_id.replace("'", "'\"'\"'")
        server["args"] = [
            "-lc",
            f"AADS_SESSION_ID='{escaped_session}' python -m mcp_servers.aads_tools_bridge",
        ]
    else:
        # 빈 값을 박지 않는다. 물려받는 편이 덮어쓰는 것보다 언제나 낫다.
        server["args"] = ["-lc", "python -m mcp_servers.aads_tools_bridge"]

with open(dst_path, "w", encoding="utf-8") as dst:
    json.dump(config, dst)
PY

    docker cp "$LOCAL_MCP_CONFIG" "${CONTAINER_NAME}:${CONTAINER_MCP_CONFIG}" >/dev/null
    args[$next_index]="$CONTAINER_MCP_CONFIG"
    break
done

CALL_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
docker_args=(exec -i -e "AADS_CLAUDE_CALL_ID=${CALL_ID}")
if [[ -n "$CONTAINER_CREDENTIAL_HOME" ]]; then
    docker_args+=(
        -e "CLAUDE_SLOT_CREDENTIAL_MODE=1"
        -e "HOME=${CONTAINER_CREDENTIAL_HOME}"
        -e "CLAUDE_CODE_OAUTH_TOKEN="
        -e "ANTHROPIC_AUTH_TOKEN="
    )
elif [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    docker_args+=(-e "CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN}")
fi

# 백그라운드 + wait: 포그라운드 자식이 돌고 있으면 bash 는 TERM trap 을 자식이 끝난 뒤에야
# 실행한다. 취소·타임아웃에서 정리가 건너뛰어지지 않도록 하고, stdin 은 3번 fd 로 넘긴다.
exec 3<&0
env "AADS_CLAUDE_CALL_ID=${CALL_ID}" docker "${docker_args[@]}" "$CONTAINER_NAME" "$CONTAINER_WRAPPER" "${args[@]}" <&3 3<&- &
CHILD_PID=$!
exec 3<&-
set +e
wait "$CHILD_PID"
status=$?
set -e
CHILD_PID=""
exit "$status"
