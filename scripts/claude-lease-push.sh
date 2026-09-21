#!/bin/bash
# claude-lease-push.sh — contabo116 이 원격 서버에 "임시 출입증(accessToken)" 만 밀어 넣는다.
#
# 설계 계약 (AADS-RUNNER-SLOT-LEASE / B안):
#   * refreshToken 은 이 서버 밖으로 나가지 않는다. 원격은 갱신 능력이 없는 소비자다.
#   * 원격 자격증명은 만료되면 그냥 죽는다 — 갱신 경합(refresh rotation)이 원천적으로 없다.
#   * 따라서 A안(파일 통째 복사)의 상호 무효화 위험이 구조적으로 제거된다.
#
# 사용: claude-lease-push.sh            (systemd timer 가 10분마다 호출)
#       CLAUDE_LEASE_TARGETS="root@1.2.3.4" claude-lease-push.sh
set -euo pipefail

SLOT_ROOT="${CLAUDE_RELAY_SLOT_HOME_ROOT:-/root/.claude-relay-slots}"
SLOTS="${CLAUDE_LEASE_SLOTS:-1 2 3 4}"
TARGETS="${CLAUDE_LEASE_TARGETS:-root@5.104.86.14 root@114.207.244.86 root@5.104.85.244}"
LEASE_ROOT="${CLAUDE_LEASE_REMOTE_ROOT:-/root/.claude-lease}"
MIN_REMAIN="${CLAUDE_LEASE_MIN_REMAINING_SEC:-300}"
LOG_FILE="${CLAUDE_LEASE_LOG:-/root/aads/logs/claude-lease-push.log}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)

mkdir -p -- "$(dirname "$LOG_FILE")"

log() {
    printf '%s %s\n' "$(date '+%F %T %Z')" "$*" >>"$LOG_FILE"
    printf '%s %s\n' "$(date '+%F %T %Z')" "$*" >&2
}

# 슬롯 자격증명에서 accessToken 만 뽑아 lease JSON 을 stdout 으로 낸다.
# 종료코드 2=토큰 없음, 3=만료시각 파싱 불가, 4=잔여시간 부족(밀어봐야 곧 죽음).
emit_lease() {
    python3 - "$1" "$MIN_REMAIN" <<'PY'
import json
import sys
import time

path, min_remain = sys.argv[1], float(sys.argv[2])
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)
oauth = payload.get("claudeAiOauth", payload)
token = oauth.get("accessToken")
if not isinstance(token, str) or not token:
    raise SystemExit(2)
raw = oauth.get("expiresAt")
try:
    raw = float(raw)
except (TypeError, ValueError):
    raise SystemExit(3)
if raw > 100_000_000_000:          # 밀리초
    expires_sec, expires_ms = raw / 1000.0, raw
else:                               # 초
    expires_sec, expires_ms = raw, raw * 1000.0
if expires_sec - time.time() < min_remain:
    raise SystemExit(4)
json.dump(
    {
        "claudeAiOauth": {
            "accessToken": token,
            "expiresAt": int(expires_ms),
            "scopes": oauth.get("scopes", []),
            "subscriptionType": oauth.get("subscriptionType", "max"),
            "leasedFrom": "contabo116",
            "leasedAt": int(time.time() * 1000),
        }
    },
    sys.stdout,
)
PY
}

overall_rc=0
pushed=0

for slot in $SLOTS; do
    cred="${SLOT_ROOT}/slot${slot}/.claude/.credentials.json"
    if [[ ! -f "$cred" ]]; then
        log "SKIP slot=${slot} reason=missing_credential path=${cred}"
        continue
    fi

    lease=""
    if ! lease="$(emit_lease "$cred")"; then
        rc=$?
        case "$rc" in
            2) reason="no_access_token" ;;
            3) reason="unparsable_expires_at" ;;
            4) reason="expires_within_${MIN_REMAIN}s" ;;
            *) reason="emit_failed_rc${rc}" ;;
        esac
        log "SKIP slot=${slot} reason=${reason}"
        overall_rc=1
        continue
    fi

    for target in $TARGETS; do
        # 토큰은 stdin 으로만 흐른다 — argv/ps 와 로컬 디스크에 남기지 않는다.
        if printf '%s' "$lease" | ssh "${SSH_OPTS[@]}" "$target" \
            "umask 077; mkdir -p ${LEASE_ROOT}/slot${slot}/.claude; \
             tmp=\$(mktemp ${LEASE_ROOT}/slot${slot}/.claude/.credentials.json.XXXXXX) || exit 65; \
             cat >\"\$tmp\"; chmod 600 \"\$tmp\"; \
             if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); o=d[\"claudeAiOauth\"]; raise SystemExit(0 if o[\"accessToken\"] else 1)' \"\$tmp\"; then \
                 mv -f \"\$tmp\" ${LEASE_ROOT}/slot${slot}/.claude/.credentials.json; \
             else rm -f \"\$tmp\"; exit 65; fi"
        then
            log "PUSH ok slot=${slot} target=${target}"
            pushed=$((pushed + 1))
        else
            log "PUSH fail slot=${slot} target=${target} rc=$?"
            overall_rc=1
        fi
    done
    unset lease
done

log "DONE pushed=${pushed} rc=${overall_rc}"
exit "$overall_rc"
