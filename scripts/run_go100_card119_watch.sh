#!/usr/bin/env bash
set -euo pipefail

LABEL="${1:-5m-check}"
CHAT_FLAG="${2:-}"
SESSION_ID="d19a0e9e-f96f-4c83-8367-20de50762364"
TODAY_KST="$(TZ=Asia/Seoul date +%Y%m%d)"

if [[ "${TODAY_KST}" != "20260803" ]]; then
  exit 0
fi

ARGS=(
  python
  scripts/go100_card119_chat_watch.py
  --session-id "${SESSION_ID}"
  --label "${LABEL}"
  --start "07:55"
  --end "15:35"
)

if [[ "${CHAT_FLAG}" == "--chat" ]]; then
  ARGS+=(--chat)
fi

exec docker exec -e PYTHONPATH=/app -w /app aads-server "${ARGS[@]}"
