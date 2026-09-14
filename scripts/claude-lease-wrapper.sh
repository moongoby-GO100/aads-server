#!/bin/bash
# claude-lease-wrapper.sh — 임시 출입증(accessToken) 전용 실행 래퍼. 원격 서버(contabo14 /
# cafe24_114)에 배치된다.
#
# claude-slot-credentials-wrapper.sh 와의 차이 (의도된 것):
#   * refreshToken 을 요구하지 않는다  — 원격에는 애초에 없다.
#   * flock 을 걸지 않는다            — 갱신하지 않으므로 직렬화할 쓰기가 없다.
#   * 역동기화(sync back)를 하지 않는다 — 원격이 만든 자격증명을 정본으로 올리지 않는다.
#   * 만료되면 78 로 죽는다           — 러너가 .env 고정 토큰 경로로 폴백하게 둔다.
set -euo pipefail

LEASE_ROOT="${CLAUDE_LEASE_ROOT:-/root/.claude-lease}"
SLOT="${CLAUDE_OAUTH_SLOT:-}"
GRACE_SEC="${CLAUDE_LEASE_MIN_REMAINING_SEC:-60}"
TEMP_HOME=""

cleanup() {
    local exit_code=$?
    trap - EXIT
    [[ -n "$TEMP_HOME" ]] && rm -rf -- "$TEMP_HOME"
    exit "$exit_code"
}
trap cleanup EXIT

lease_valid() {
    python3 - "$1" "$GRACE_SEC" <<'PY'
import json
import sys
import time

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    raise SystemExit(1)
oauth = payload.get("claudeAiOauth", payload)
if not isinstance(oauth, dict):
    raise SystemExit(1)
token = oauth.get("accessToken")
if not isinstance(token, str) or not token:
    raise SystemExit(1)
raw = oauth.get("expiresAt")
try:
    raw = float(raw)
except (TypeError, ValueError):
    raise SystemExit(1)
if raw > 100_000_000_000:
    raw /= 1000.0
raise SystemExit(0 if raw - time.time() > float(sys.argv[2]) else 1)
PY
}

# 슬롯이 지정되지 않으면 잔여 유효시간이 가장 긴 lease 를 고른다.
pick_slot() {
    local best="" candidate
    for candidate in 1 2; do
        if lease_valid "${LEASE_ROOT}/slot${candidate}/.claude/.credentials.json" 2>/dev/null; then
            best="$candidate"
            break
        fi
    done
    printf '%s' "$best"
}

if [[ -z "$SLOT" ]]; then
    SLOT="$(pick_slot)"
fi
[[ -n "$SLOT" ]] || {
    echo "no valid claude lease found under ${LEASE_ROOT} (push from contabo116 first)" >&2
    exit 78
}

CREDENTIAL_FILE="${CLAUDE_SLOT_CREDENTIALS_FILE:-${LEASE_ROOT}/slot${SLOT}/.claude/.credentials.json}"

[[ -f "$CREDENTIAL_FILE" ]] || {
    echo "lease credential file is missing (slot=${SLOT}) path=${CREDENTIAL_FILE}" >&2
    exit 78
}
lease_valid "$CREDENTIAL_FILE" || {
    echo "lease is expired or incomplete (slot=${SLOT}) — contabo116 push 가 필요하다" >&2
    exit 78
}

TEMP_HOME="$(mktemp -d "/tmp/claude-lease-${SLOT}.XXXXXX")"
mkdir -p -- "$TEMP_HOME/.claude"
cp -- "$CREDENTIAL_FILE" "$TEMP_HOME/.claude/.credentials.json"
chmod 700 -- "$TEMP_HOME" "$TEMP_HOME/.claude"
chmod 600 -- "$TEMP_HOME/.claude/.credentials.json"
[[ -f "$(dirname "$CREDENTIAL_FILE")/settings.json" ]] \
    && cp -- "$(dirname "$CREDENTIAL_FILE")/settings.json" "$TEMP_HOME/.claude/settings.json"

export HOME="$TEMP_HOME"
export CLAUDE_OAUTH_SLOT="$SLOT"
# 고정 토큰이 남아 있으면 우선순위가 뒤집힌다 — lease 경로에서는 반드시 지운다.
unset CLAUDE_CODE_OAUTH_TOKEN 2>/dev/null || true
unset ANTHROPIC_API_KEY 2>/dev/null || true
unset ANTHROPIC_BASE_URL 2>/dev/null || true

# exec 을 쓰지 않는다 — cleanup 이 임시 HOME 을 반드시 지우게 한다.
"$@"
