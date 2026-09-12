#!/bin/bash
set -euo pipefail

# Host CLI credential isolation.  The lock is per account, so the two OAuth
# accounts can refresh concurrently while refresh-token reuse within one slot
# is serialized.
CREDENTIAL_FILE="${CLAUDE_SLOT_CREDENTIALS_FILE:?missing slot credential file}"
LOCK_FILE="${CLAUDE_SLOT_CREDENTIAL_LOCK_FILE:-${CREDENTIAL_FILE}.lock}"
SLOT="${CLAUDE_OAUTH_SLOT:-unknown}"
REFRESH_LOCK_WINDOW_SEC="${CLAUDE_SLOT_REFRESH_LOCK_WINDOW_SEC:-6000}"
TEMP_HOME=""
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

sync_credential() {
    local candidate="${TEMP_HOME}/.claude/.credentials.json"
    local destination_dir
    local staged
    [[ -f "$candidate" ]] || return 0
    validate_credential "$candidate" || return 0
    cmp -s "$candidate" "$CREDENTIAL_FILE" && return 0
    if [[ "$LOCK_MODE" == "shared" ]]; then
        exec 8>"${LOCK_FILE}.sync"
        chmod 600 "${LOCK_FILE}.sync"
        flock -x 8
        if [[ "$(sha256sum "$CREDENTIAL_FILE" | awk '{print $1}')" != "$ORIGINAL_CREDENTIAL_DIGEST" ]]; then
            return 0
        fi
    fi
    destination_dir="$(dirname "$CREDENTIAL_FILE")"
    staged="$(mktemp "${destination_dir}/.credentials.json.XXXXXX.tmp")"
    cp -- "$candidate" "$staged"
    chmod 600 "$staged"
    if ! validate_credential "$staged"; then
        rm -f -- "$staged"
        return 0
    fi
    mv -f -- "$staged" "$CREDENTIAL_FILE"
}

cleanup() {
    local exit_code=$?
    trap - EXIT
    set +e
    if [[ -n "$TEMP_HOME" ]]; then
        sync_credential
        rm -rf -- "$TEMP_HOME"
    fi
    exit "$exit_code"
}
trap cleanup EXIT

[[ -f "$CREDENTIAL_FILE" ]] || {
    echo "slot credential file is missing (slot=${SLOT})" >&2
    exit 78
}
command -v flock >/dev/null 2>&1 || {
    echo "flock is required for slot credential serialization" >&2
    exit 78
}

mkdir -p -- "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
chmod 600 "$LOCK_FILE"
if credential_requires_exclusive_lock "$CREDENTIAL_FILE"; then
    flock -x 9
else
    LOCK_MODE="shared"
    flock -s 9
fi

validate_credential "$CREDENTIAL_FILE" || {
    echo "slot credential file is incomplete (slot=${SLOT})" >&2
    exit 78
}
ORIGINAL_CREDENTIAL_DIGEST="$(sha256sum "$CREDENTIAL_FILE" | awk '{print $1}')"
TEMP_HOME="$(mktemp -d "/tmp/claude-slot-${SLOT}.XXXXXX")"
mkdir -p -- "$TEMP_HOME/.claude"
cp -- "$CREDENTIAL_FILE" "$TEMP_HOME/.claude/.credentials.json"
chmod 700 "$TEMP_HOME" "$TEMP_HOME/.claude"
chmod 600 "$TEMP_HOME/.claude/.credentials.json"
[[ -f "$(dirname "$CREDENTIAL_FILE")/settings.json" ]] \
    && cp -- "$(dirname "$CREDENTIAL_FILE")/settings.json" "$TEMP_HOME/.claude/settings.json"

export HOME="$TEMP_HOME"
unset CLAUDE_CODE_OAUTH_TOKEN
"$@"
