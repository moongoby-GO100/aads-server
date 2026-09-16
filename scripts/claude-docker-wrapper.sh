#!/bin/bash
set -euo pipefail

CONTAINER_NAME="${CLAUDE_DOCKER_CONTAINER:-aads-server}"
CONTAINER_WRAPPER="${CLAUDE_DOCKER_WRAPPER:-/app/scripts/claude-oauth-wrapper.sh}"

LOCAL_MCP_CONFIG=""
CONTAINER_MCP_CONFIG=""
CREDENTIAL_FILE="${CLAUDE_SLOT_CREDENTIALS_FILE:-}"
CREDENTIAL_LOCK_FILE="${CLAUDE_SLOT_CREDENTIAL_LOCK_FILE:-}"
OAUTH_SLOT="${CLAUDE_OAUTH_SLOT:-}"
# 만료까지 이 시간 안으로 들어오면 배타 잠금을 잡는다 — 갱신이 겹쳐 refresh
# token 이 서로를 무효화하는 것을 막기 위해서다.
#
# 기본값이 6000초(100분)였다. 토큰 수명이 약 8시간이니 **수명의 20%** 구간에서
# 배타 잠금이 걸린다는 뜻이고, 그동안 같은 슬롯의 다른 호출은 전부 줄을 선다.
# 2026-09-16 그 구간에 걸린 호출 하나가 6분 48초를 쥐고 있어 신규 채팅창까지
# 응답하지 못했다. 갱신에 실제로 필요한 것은 몇 초다. 600초면 충분하고,
# 배타 구간이 수명의 2% 로 줄어든다.
REFRESH_LOCK_WINDOW_SEC="${CLAUDE_SLOT_REFRESH_LOCK_WINDOW_SEC:-600}"
CONTAINER_CREDENTIAL_HOME=""
# 대화 기록 영속 저장소(컨테이너 안 경로). /root/aads/data/claude-sessions 가
# 여기에 마운트돼 있어 컨테이너 교체를 넘어 살아남는다.
CLAUDE_SESSION_STORE="${CLAUDE_SESSION_STORE:-/tmp/.claude-sdk/.claude}"
LOCK_MODE="exclusive"
ORIGINAL_CREDENTIAL_DIGEST=""

validate_credential() {
    python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
oauth = payload.get("claudeAiOauth", payload)
if not isinstance(oauth, dict):
    raise SystemExit(1)
if not isinstance(oauth.get("accessToken"), str) or not oauth["accessToken"]:
    raise SystemExit(1)
if not isinstance(oauth.get("refreshToken"), str) or not oauth["refreshToken"]:
    raise SystemExit(1)
PY
}

credential_requires_exclusive_lock() {
    if [[ "${CLAUDE_SLOT_FORCE_EXCLUSIVE_LOCK:-0}" == "1" ]]; then
        return 0
    fi
    python3 - "$1" "$REFRESH_LOCK_WINDOW_SEC" <<'PY'
import json
import sys
import time

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
oauth = payload.get("claudeAiOauth", payload)
expires_at = oauth.get("expiresAt") if isinstance(oauth, dict) else None
try:
    expires_at = float(expires_at)
    if expires_at > 100_000_000_000:
        expires_at /= 1000.0
except (TypeError, ValueError):
    raise SystemExit(0)
window = max(0.0, float(sys.argv[2]))
raise SystemExit(0 if expires_at <= time.time() + window else 1)
PY
}

sync_container_credential() {
    local destination_dir
    local staged
    [[ -n "$CONTAINER_CREDENTIAL_HOME" && -n "$CREDENTIAL_FILE" ]] || return 0
    destination_dir="$(dirname "$CREDENTIAL_FILE")"
    staged="$(mktemp "${destination_dir}/.credentials.json.XXXXXX.tmp")"
    if ! docker cp \
        "${CONTAINER_NAME}:${CONTAINER_CREDENTIAL_HOME}/.claude/.credentials.json" \
        "$staged" >/dev/null 2>&1; then
        rm -f -- "$staged"
        return 0
    fi
    chmod 600 "$staged"
    if ! validate_credential "$staged"; then
        rm -f -- "$staged"
        return 0
    fi
    if cmp -s "$staged" "$CREDENTIAL_FILE"; then
        rm -f -- "$staged"
        return 0
    fi
    if [[ "$LOCK_MODE" == "shared" ]]; then
        exec 8>"${CREDENTIAL_LOCK_FILE}.sync"
        chmod 600 "${CREDENTIAL_LOCK_FILE}.sync"
        flock -x 8
        if [[ "$(sha256sum "$CREDENTIAL_FILE" | awk '{print $1}')" != "$ORIGINAL_CREDENTIAL_DIGEST" ]]; then
            rm -f -- "$staged"
            return 0
        fi
    fi
    mv -f -- "$staged" "$CREDENTIAL_FILE"
}

cleanup() {
    local exit_code=$?
    trap - EXIT
    set +e
    sync_container_credential
    if [[ -n "$CONTAINER_CREDENTIAL_HOME" ]]; then
        docker exec "$CONTAINER_NAME" sh -lc \
            "rm -rf '$CONTAINER_CREDENTIAL_HOME'" >/dev/null 2>&1 || true
    fi
    if [[ -n "$CONTAINER_MCP_CONFIG" ]]; then
        docker exec "$CONTAINER_NAME" sh -lc "rm -f '$CONTAINER_MCP_CONFIG'" >/dev/null 2>&1 || true
    fi
    if [[ -n "$LOCAL_MCP_CONFIG" ]]; then
        rm -f "$LOCAL_MCP_CONFIG" >/dev/null 2>&1 || true
    fi
    exit "$exit_code"
}
trap cleanup EXIT

args=("$@")

if [[ -n "$CREDENTIAL_FILE" ]]; then
    [[ -f "$CREDENTIAL_FILE" ]] || {
        echo "slot credential file is missing (slot=${OAUTH_SLOT:-unknown})" >&2
        exit 78
    }
    validate_credential "$CREDENTIAL_FILE" || {
        echo "slot credential file is incomplete (slot=${OAUTH_SLOT:-unknown})" >&2
        exit 78
    }
    command -v flock >/dev/null 2>&1 || {
        echo "flock is required for slot credential serialization" >&2
        exit 78
    }
    CREDENTIAL_LOCK_FILE="${CREDENTIAL_LOCK_FILE:-${CREDENTIAL_FILE}.lock}"
    mkdir -p -- "$(dirname "$CREDENTIAL_LOCK_FILE")"
    exec 9>"$CREDENTIAL_LOCK_FILE"
    chmod 600 "$CREDENTIAL_LOCK_FILE"
    # 잠금 대기에 반드시 상한을 둔다.
    #
    # 2026-09-16 실측: 토큰 갱신이 필요해 배타 잠금을 잡은 호출 하나가 **모델
    # 호출이 끝날 때까지** 잠금을 쥐고 있었다(6분 48초). 같은 슬롯을 쓰는 다른
    # 세션 9개가 전부 flock -s 에서 막혀, 신규 채팅창조차 응답하지 못했다.
    # 무한 대기는 한 호출의 지연을 전체 장애로 키운다.
    #
    # 배타를 못 잡으면 다른 호출이 이미 갱신 중이라는 뜻이다. 그 결과를 쓰면
    # 되므로 공유로 내려간다. 공유마저 시간이 차면 잠금 없이 진행한다 —
    # 자격증명은 위에서 이미 검증했고, 되쓰기는 .sync 잠금과 digest 비교가
    # 따로 지킨다(sync_container_credential). 막혀서 못 하는 것보다 낫다.
    _excl_wait="${CLAUDE_SLOT_EXCLUSIVE_LOCK_WAIT_SEC:-300}"
    _shared_wait="${CLAUDE_SLOT_SHARED_LOCK_WAIT_SEC:-120}"
    if credential_requires_exclusive_lock "$CREDENTIAL_FILE"; then
        if ! flock -x -w "$_excl_wait" 9; then
            echo "slot${OAUTH_SLOT:-?}: exclusive lock wait exceeded ${_excl_wait}s — 공유 모드로 진행" >&2
            LOCK_MODE="shared"
            flock -s -w "$_shared_wait" 9 \
                || echo "slot${OAUTH_SLOT:-?}: shared lock wait exceeded ${_shared_wait}s — 잠금 없이 진행" >&2
        fi
    else
        LOCK_MODE="shared"
        if ! flock -s -w "$_shared_wait" 9; then
            echo "slot${OAUTH_SLOT:-?}: shared lock wait exceeded ${_shared_wait}s — 잠금 없이 진행" >&2
        fi
    fi
    ORIGINAL_CREDENTIAL_DIGEST="$(sha256sum "$CREDENTIAL_FILE" | awk '{print $1}')"

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

    docker cp "$CREDENTIAL_FILE" \
        "${CONTAINER_NAME}:${CONTAINER_CREDENTIAL_HOME}/.claude/.credentials.json" >/dev/null
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

docker_args=(exec -i)
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

docker "${docker_args[@]}" "$CONTAINER_NAME" "$CONTAINER_WRAPPER" "${args[@]}"
