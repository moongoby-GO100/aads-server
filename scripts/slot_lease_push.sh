#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AADS-RUNNER-SLOT-LEASE — 릴레이 슬롯 accessToken 주기 대여 (B안)
#
# 왜 필요한가
#   contabo14 / cafe24_114 의 러너와 터미널은 .env 고정 oat 토큰만 쓴다.
#   2026-09-14 실측: 계정1 429(주간한도), 계정2 401(revoked) 로 두 서버의
#   Claude 경로가 전멸했다. 같은 시각 contabo116 릴레이 슬롯은 정상이었다 —
#   슬롯은 refreshToken 을 들고 CLI 가 스스로 갱신하기 때문이다.
#
# 무엇을 보내고 무엇을 안 보내는가 (이 스크립트의 존재 이유)
#   보낸다  : accessToken (수명 약 2시간, 만료되면 그냥 죽는 임시 출입증)
#   안 보낸다: refreshToken (새 출입증을 무한 발급하는 영구 열쇠)
#   refreshToken 이 페이로드에 섞이면 assert 로 즉시 중단한다 (send_guard).
#
#   파일 통째 복사(A안)를 쓰지 않는 이유: 3대가 각자 refresh 를 돌리면
#   refresh 회전이 경합해 서로를 무효화한다. flock 은 호스트 로컬이라
#   서버 간 조정이 불가능하다. 갱신 권한은 contabo116 단독으로 남긴다.
#
# 대상 파일
#   원격 /root/.claude/lease.env (0600) — 러너는 /root/scripts/runner.env 경유로,
#   터미널은 /usr/local/bin/claude 래퍼 경유로 이 파일을 읽는다.
#
# 운영
#   cron */15. 토큰 수명이 2시간이므로 한두 번 실패해도 끊기지 않는다.
#   만료 5분 이내 토큰은 보내지 않는다(원격에서 쓰는 도중 죽는 것을 막는다).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SLOT_ROOT="${CLAUDE_RELAY_SLOT_HOME_ROOT:-/root/.claude-relay-slots}"
TARGETS="${SLOT_LEASE_TARGETS:-5.104.86.14 114.207.244.86 5.104.85.244}"
REMOTE_LEASE="${SLOT_LEASE_REMOTE_PATH:-/root/.claude/lease.env}"
MIN_REMAINING_SEC="${SLOT_LEASE_MIN_REMAINING_SEC:-300}"
SSH_TIMEOUT="${SLOT_LEASE_SSH_TIMEOUT:-25}"
LOCK_FILE="/var/run/aads-slot-lease.lock"
DRY_RUN="${SLOT_LEASE_DRY_RUN:-0}"

log() { printf '%s [slot-lease] %s\n' "$(date '+%F %T %Z')" "$*"; }

# ── 동시 실행 방지 (R-BG: 내가 띄운 것은 내가 회수한다) ────────────────────
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "SKIP 이전 실행이 아직 진행 중이다"
    exit 0
fi

# ── 1) 슬롯에서 accessToken 만 추출 ────────────────────────────────────────
payload="$(python3 - "$SLOT_ROOT" "$MIN_REMAINING_SEC" <<'PY'
import json
import os
import sys
import time

slot_root = sys.argv[1]
min_remaining = int(sys.argv[2])
now_ms = int(time.time() * 1000)

tokens = {}
expiries = []
skipped = []

for slot in (1, 2):
    path = os.path.join(slot_root, f"slot{slot}", ".claude", ".credentials.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            oauth = json.load(handle)
    except Exception as exc:  # 슬롯이 없거나 깨졌으면 그 슬롯만 건너뛴다
        skipped.append(f"slot{slot}:unreadable({exc.__class__.__name__})")
        continue
    oauth = oauth.get("claudeAiOauth", oauth)
    if not isinstance(oauth, dict):
        skipped.append(f"slot{slot}:shape")
        continue

    access = oauth.get("accessToken")
    expires = oauth.get("expiresAt")
    if not isinstance(access, str) or not access:
        skipped.append(f"slot{slot}:no_access_token")
        continue
    if not isinstance(expires, (int, float)):
        skipped.append(f"slot{slot}:no_expiry")
        continue
    remaining = (expires - now_ms) / 1000.0
    if remaining < min_remaining:
        # 원격에서 쓰는 도중 죽을 토큰은 애초에 보내지 않는다
        skipped.append(f"slot{slot}:expiring({int(remaining)}s)")
        continue

    tokens[slot] = access
    expiries.append(int(expires // 1000))

if not tokens:
    sys.stderr.write("NO_USABLE_SLOT " + ",".join(skipped) + "\n")
    raise SystemExit(3)

lines = [
    "# AADS slot lease — contabo116 이 발급한 임시 accessToken 이다.",
    "# 손으로 고치지 마라. */15 cron 이 덮어쓴다.",
    "# 영구 갱신 열쇠는 이 파일에 절대 들어오지 않는다 — send_guard 가 차단한다.",
    "# (문구에 필드명을 적지 않는다: 감사용 grep 이 주석을 유출로 오탐한다)",
    f'export ANTHROPIC_AUTH_TOKEN="{tokens.get(1, tokens.get(2))}"',
]
if 2 in tokens:
    lines.append(f'export ANTHROPIC_AUTH_TOKEN_2="{tokens[2]}"')
elif 1 in tokens:
    lines.append(f'export ANTHROPIC_AUTH_TOKEN_2="{tokens[1]}"')

lines += [
    f'export AADS_SLOT_LEASE_EXPIRES_AT="{min(expiries)}"',
    f'export AADS_SLOT_LEASE_ISSUED_AT="{int(time.time())}"',
    'export AADS_SLOT_LEASE_SOURCE="contabo116"',
    f'export AADS_SLOT_LEASE_SLOTS="{",".join(str(s) for s in sorted(tokens))}"',
    "",
]
sys.stdout.write("\n".join(lines))
sys.stderr.write(
    "slots=%s skipped=%s\n" % (",".join(str(s) for s in sorted(tokens)), ",".join(skipped) or "-")
)
PY
)" || { log "FATAL 사용 가능한 슬롯이 없다 — 대여를 건너뛴다"; exit 3; }

# ── 2) send_guard: 영구 열쇠가 섞였으면 무조건 중단 ────────────────────────
# 이 검사가 B안과 A안을 가르는 유일한 지점이다. 절대 완화하지 마라.
# 주석 줄은 제외한다 — 파일 머리말이 이 필드명을 설명하느라 언급하기 때문이다.
# 실제 유출이라면 값은 export 줄에 실린다.
if printf '%s' "$payload" | grep -v '^[[:space:]]*#' | grep -qi 'refreshToken\|refresh_token'; then
    log "FATAL send_guard — 페이로드에 refreshToken 이 섞였다. 전송 중단."
    exit 4
fi
if ! printf '%s' "$payload" | grep -q '^export ANTHROPIC_AUTH_TOKEN='; then
    log "FATAL 페이로드에 토큰이 없다. 전송 중단."
    exit 5
fi

lease_expires="$(printf '%s' "$payload" | sed -n 's/^export AADS_SLOT_LEASE_EXPIRES_AT="\([0-9]*\)"$/\1/p')"
log "lease 준비 완료 — 만료 $(date -d "@${lease_expires}" '+%F %T %Z') (남은 $(( lease_expires - $(date +%s) ))s)"

if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN=1 — 전송하지 않는다"
    exit 0
fi

# ── 3) 대상 서버로 원자적 배치 ─────────────────────────────────────────────
rc_all=0
for host in $TARGETS; do
    if printf '%s' "$payload" | timeout "$SSH_TIMEOUT" ssh \
            -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 \
            "root@${host}" \
            "umask 077; mkdir -p \$(dirname ${REMOTE_LEASE}); cat > ${REMOTE_LEASE}.tmp && chmod 600 ${REMOTE_LEASE}.tmp && mv -f ${REMOTE_LEASE}.tmp ${REMOTE_LEASE}" \
            2>/dev/null; then
        log "OK   ${host} ← lease 배치"
    else
        log "FAIL ${host} — 전송 실패 (기존 lease 유지)"
        rc_all=1
    fi
done

exit "$rc_all"
