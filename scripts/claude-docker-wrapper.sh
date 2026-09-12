#!/bin/bash
set -euo pipefail

CONTAINER_NAME="${CLAUDE_DOCKER_CONTAINER:-aads-server}"
CONTAINER_WRAPPER="${CLAUDE_DOCKER_WRAPPER:-/app/scripts/claude-oauth-wrapper.sh}"

LOCAL_MCP_CONFIG=""
CONTAINER_MCP_CONFIG=""
CREDENTIAL_FILE="${CLAUDE_SLOT_CREDENTIALS_FILE:-}"
CREDENTIAL_LOCK_FILE="${CLAUDE_SLOT_CREDENTIAL_LOCK_FILE:-}"
OAUTH_SLOT="${CLAUDE_OAUTH_SLOT:-}"
REFRESH_LOCK_WINDOW_SEC="${CLAUDE_SLOT_REFRESH_LOCK_WINDOW_SEC:-6000}"
CONTAINER_CREDENTIAL_HOME=""
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
    if credential_requires_exclusive_lock "$CREDENTIAL_FILE"; then
        flock -x 9
    else
        LOCK_MODE="shared"
        flock -s 9
    fi
    ORIGINAL_CREDENTIAL_DIGEST="$(sha256sum "$CREDENTIAL_FILE" | awk '{print $1}')"

    CONTAINER_CREDENTIAL_HOME="/tmp/.claude-relay-slot-${OAUTH_SLOT:-unknown}-${BASHPID}-${RANDOM}"
    docker exec "$CONTAINER_NAME" sh -lc \
        "umask 077; mkdir -p '$CONTAINER_CREDENTIAL_HOME/.claude'" >/dev/null
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
    session_id = ""
    for idx, arg in enumerate(args[:-1]):
        if arg == "-e" and args[idx + 1].startswith("AADS_SESSION_ID="):
            session_id = args[idx + 1].split("=", 1)[1]
            break
    escaped_session = session_id.replace("'", "'\"'\"'")
    server["command"] = "sh"
    server["args"] = [
        "-lc",
        f"AADS_SESSION_ID='{escaped_session}' python -m mcp_servers.aads_tools_bridge",
    ]

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
