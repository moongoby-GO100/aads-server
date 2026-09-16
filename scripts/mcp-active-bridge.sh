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

# 상주 도구 모드 설정을 컨테이너로 넘긴다. 호스트에만 두면 브리지가 못 읽는다.
# 기본은 비어 있고, 그 경우 브리지가 기존 동작(전량 노출)을 한다.
# 설계: aads-docs/docs/PRD-TOOL-RESIDENT-SET-v1.0.md
exec docker exec -i \
    -e "AADS_SESSION_ID=${AADS_SESSION_ID:-default}" \
    -e "AADS_TOOL_RESIDENT_MODE=${AADS_TOOL_RESIDENT_MODE:-0}" \
    -e "AADS_TOOL_RESIDENT_LIST=${AADS_TOOL_RESIDENT_LIST:-}" \
    "$CONTAINER_NAME" \
    python3 -m mcp_servers.aads_tools_bridge
