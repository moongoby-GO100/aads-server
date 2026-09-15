#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ACTIVE_CONTAINER_FILE="${CLAUDE_ACTIVE_CONTAINER_FILE:-${REPO_ROOT}/.active_container}"
CONTAINER_WRAPPER="${CLAUDE_DOCKER_WRAPPER:-/app/scripts/claude-oauth-wrapper.sh}"

resolve_container_name() {
    if [[ -n "${CLAUDE_DOCKER_CONTAINER:-}" ]]; then
        printf '%s\n' "${CLAUDE_DOCKER_CONTAINER}"
        return
    fi
    if [[ -f "$ACTIVE_CONTAINER_FILE" ]]; then
        local active_container
        active_container="$(tr -d '[:space:]' < "$ACTIVE_CONTAINER_FILE")"
        if [[ -n "$active_container" ]]; then
            printf '%s\n' "$active_container"
            return
        fi
    fi
    printf '%s\n' "aads-server"
}

CONTAINER_NAME="$(resolve_container_name)"
LOCAL_MCP_CONFIG=""
CONTAINER_MCP_CONFIG=""

# Slot credentials need refresh-token locking and atomic copy-back. Delegate
# only that mode to the canonical Docker wrapper while preserving this file's
# active blue/green container resolution and the legacy env-token path below.
if [[ -n "${CLAUDE_SLOT_CREDENTIALS_FILE:-}" ]]; then
    export CLAUDE_DOCKER_CONTAINER="$CONTAINER_NAME"
    exec "${SCRIPT_DIR}/claude-docker-wrapper.sh" "$@"
fi

cleanup() {
    local exit_code=$?
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
if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    docker_args+=(-e "CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN}")
fi

docker "${docker_args[@]}" "$CONTAINER_NAME" "$CONTAINER_WRAPPER" "${args[@]}"
