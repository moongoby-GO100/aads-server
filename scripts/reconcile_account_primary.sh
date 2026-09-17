#!/usr/bin/env bash
# 주계정 조정기 — 2분마다.
#
# 자동 모드면 "곧 리셋될 한도부터 태운다" 규칙으로 llm_api_keys.priority 를
# 다시 매긴다(2026-09-17 대표님 지시). 수동 모드면 손대지 않는다.
#
# 그다음 코덱스 state.json 을 DB 기준으로 내린다. 릴레이는 그 파일만 읽으므로,
# 이게 없으면 수동으로 주계정을 바꿔도 코덱스는 최대 10분(기존 --sync 크론
# 주기) 동안 옛 계정을 계속 쓴다.
set -uo pipefail

STATE_DIR="/root/aads/aads-server"
PORT="$(cat "${STATE_DIR}/.active_port" 2>/dev/null || echo 8100)"
[[ "$PORT" =~ ^[0-9]+$ ]] || PORT=8100

log() { echo "$(TZ=Asia/Seoul date '+%F %T KST') $*"; }

resp="$(curl -s --max-time 20 -X POST \
    "http://127.0.0.1:${PORT}/api/v1/ops/account-primary/reconcile" 2>/dev/null || true)"
if [[ -z "$resp" ]]; then
    log "조정 API 무응답 (port=${PORT}) — state.json 갱신만 진행한다"
else
    changed="$(printf '%s' "$resp" | python3 -c '
import json, sys
try:
    out = json.load(sys.stdin).get("results", [])
except Exception:
    sys.exit(0)
for r in out:
    if r.get("changed"):
        print("%s: 주계정 → %s" % (r.get("provider"), r.get("primary")))
' 2>/dev/null)"
    [[ -n "$changed" ]] && log "$changed"
fi

# 사용량 수집 없이 DB 값만 내린다 — 2분 주기에 CLI 를 돌리면 안 된다.
timeout 60 /usr/bin/python3 "${STATE_DIR}/scripts/codex_usage.py" --state-only >/dev/null 2>&1 \
    || log "codex state.json 갱신 실패"

exit 0
