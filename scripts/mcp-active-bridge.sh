#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${AADS_SERVER_ROOT:-/root/aads/aads-server}"
ACTIVE_CONTAINER_FILE="${AADS_ACTIVE_CONTAINER_FILE:-${REPO_ROOT}/.active_container}"
CONTAINER_NAME="${AADS_MCP_CONTAINER:-}"

if [[ -z "$CONTAINER_NAME" && -f "$ACTIVE_CONTAINER_FILE" ]]; then
    CONTAINER_NAME="$(tr -d '[:space:]' < "$ACTIVE_CONTAINER_FILE")"
fi

if [[ -z "$CONTAINER_NAME" ]]; then
    CONTAINER_NAME="aads-server"
fi

exec docker exec -i \
    -e "AADS_SESSION_ID=${AADS_SESSION_ID:-default}" \
    "$CONTAINER_NAME" \
    python3 -m mcp_servers.aads_tools_bridge
