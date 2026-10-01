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

STATE_DIR="${AADS_SERVER_DIR:-/root/aads/aads-server}"
ENV_FILE="${AADS_ENV_FILE:-${STATE_DIR}/.env}"
FAIL_STATE="${AADS_RECONCILE_FAIL_STATE:-/tmp/aads-account-primary.fail}"
PORT="$(cat "${STATE_DIR}/.active_port" 2>/dev/null || echo 8100)"
[[ "$PORT" =~ ^[0-9]+$ ]] || PORT=8100

log() { echo "$(TZ=Asia/Seoul date '+%F %T KST') $*"; }

# JWT 미들웨어(app/main.py)는 X-Monitor-Key 헤더가 있는 내부 호출을 통과시킨다.
# 키는 .env 에서만 읽는다(R-KEY).
MONITOR_KEY="${AADS_MONITOR_KEY:-}"
if [[ -z "$MONITOR_KEY" && -f "$ENV_FILE" ]]; then
    MONITOR_KEY="$(grep '^AADS_MONITOR_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]"'"'"'')"
fi

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT

code="$(curl -s --max-time 20 -o "$body_file" -w '%{http_code}' -X POST \
    -H "X-Monitor-Key: ${MONITOR_KEY}" \
    "http://127.0.0.1:${PORT}/api/v1/ops/account-primary/reconcile" 2>/dev/null || true)"
[[ "$code" =~ ^[0-9]{3}$ ]] || code="000"

parsed_ok=0
if [[ "$code" =~ ^2 ]]; then
    changed="$(python3 -c '
import json, sys
try:
    out = json.load(open(sys.argv[1])).get("results")
except Exception:
    sys.exit(3)
if not isinstance(out, list):
    sys.exit(3)
for r in out:
    if r.get("changed"):
        print("%s: 주계정 → %s" % (r.get("provider"), r.get("primary")))
' "$body_file" 2>/dev/null)" && parsed_ok=1
fi

if [[ "$parsed_ok" == 1 ]]; then
    rm -f "$FAIL_STATE"
    [[ -n "${changed:-}" ]] && log "$changed"
else
    # 같은 실패를 2분마다 되풀이해 적지 않는다 — 상태가 바뀔 때만 기록한다.
    sig="port=${PORT}, http=${code}"
    if [[ "$(cat "$FAIL_STATE" 2>/dev/null)" != "$sig" ]]; then
        log "조정 API 실패 (${sig}) — state.json 갱신만 진행한다"
        echo "$sig" > "$FAIL_STATE" 2>/dev/null || true
    fi
fi

# 사용량 수집 없이 DB 값만 내린다 — 2분 주기에 CLI 를 돌리면 안 된다.
timeout 60 /usr/bin/python3 "${STATE_DIR}/scripts/codex_usage.py" --state-only >/dev/null 2>&1 \
    || log "codex state.json 갱신 실패"

exit 0
