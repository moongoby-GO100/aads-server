#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy_lease_clients.sh — 대여 토큰 "소비자" 를 원격 서버에 설치한다.
#
# slot_lease_push.sh 가 발급자라면 이 스크립트는 수신자를 깔아 준다. 둘이 짝이다.
#
# 설치하는 것 두 개
#   1) /root/scripts/runner.env         — pipeline-runner.sh 가 매 job 마다 읽는다
#   2) /usr/local/bin/claude            — 터미널에서 `claude` 를 쳤을 때의 진입점
#
# 왜 스크립트로 남기나
#   2026-09-14 이 배치를 ssh heredoc 으로 손으로 했다. 그러면 서버가 하나 늘 때
#   아무도 무엇을 깔았는지 모른다. 같은 날 systemd 가 실행 중인 lease 스크립트
#   5개가 저장소 밖 untracked 로 떠 있는 것을 발견했다 — 재배포하면 사라진다.
#   설치를 코드로 남겨 두면 새 서버는 이 스크립트 한 번으로 끝난다.
#
# 멱등이다. 몇 번을 돌려도 같은 결과가 되고, 기존 파일은 .bak_lease_<날짜> 로 남는다.
#
#   deploy_lease_clients.sh                     # 기본 대상 3대
#   LEASE_CLIENT_TARGETS="root@1.2.3.4" deploy_lease_clients.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

TARGETS="${LEASE_CLIENT_TARGETS:-root@5.104.86.14 root@114.207.244.86 root@5.104.85.244}"
STAMP="$(date '+%Y%m%d')"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8)

log() { printf '%s [lease-clients] %s\n' "$(date '+%F %T %Z')" "$*"; }

read -r -d '' RUNNER_ENV <<'RUNNER_ENV_EOF' || true
# AADS-RUNNER-SLOT-LEASE — 손으로 고치지 마라. deploy_lease_clients.sh 가 덮어쓴다.
# contabo116 이 10분마다 /root/.claude/lease.env 에 임시 accessToken 을 배치한다.
# pipeline-runner.sh 는 current.env 다음에 이 파일을 source 하므로,
# 여기서 덮어쓴 값이 current.env 의 죽은 고정 토큰(429/401)을 이긴다.
# lease 가 없거나 만료면 기존 고정 토큰 경로로 조용히 되돌아간다.
if [ -f /root/.claude/lease.env ]; then
  . /root/.claude/lease.env || true
  case "${AADS_SLOT_LEASE_EXPIRES_AT:-0}" in
    ''|*[!0-9]*) AADS_SLOT_LEASE_EXPIRES_AT=0 ;;
  esac
  if [ "$AADS_SLOT_LEASE_EXPIRES_AT" -le "$(date +%s)" ]; then
    unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_AUTH_TOKEN_2 2>/dev/null || true
    . /root/.claude/current.env 2>/dev/null || true
  fi
fi
RUNNER_ENV_EOF

read -r -d '' CLAUDE_WRAPPER <<'WRAPPER_EOF' || true
#!/usr/bin/env bash
set -euo pipefail
# lease 경로를 바꿀 수 있게 둔다 — 만료 폴백을 운영 파일을 건드리지 않고 시험하기 위함이다.
AADS_LEASE_FILE="${AADS_LEASE_FILE:-/root/.claude/lease.env}"
# AADS-RUNNER-SLOT-LEASE (B안, 2026-09-14)
# 이 서버 터미널에서 `claude` 를 쳐도 contabo116 이 대여한 임시 accessToken 을 쓴다.
# 우선순위: 호출자 CLAUDE_CODE_OAUTH_TOKEN > lease(미만료) > current.env(레거시 폴백)
# 1번 계정이 주간한도일 때: AADS_LEASE_SLOT=2 claude -p "..."
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
  set +u
  _aads_lease_ok=0
  if [[ -f "$AADS_LEASE_FILE" ]]; then
    source "$AADS_LEASE_FILE"
    _aads_exp="${AADS_SLOT_LEASE_EXPIRES_AT:-0}"
    [[ "$_aads_exp" =~ ^[0-9]+$ ]] || _aads_exp=0
    if [[ "$_aads_exp" -gt "$(date +%s)" ]]; then
      if [[ "${AADS_LEASE_SLOT:-1}" == "2" && -n "${ANTHROPIC_AUTH_TOKEN_2:-}" ]]; then
        export CLAUDE_CODE_OAUTH_TOKEN="$ANTHROPIC_AUTH_TOKEN_2"
      else
        export CLAUDE_CODE_OAUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-}"
      fi
      [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]] && _aads_lease_ok=1
    fi
  fi
  if [[ "$_aads_lease_ok" != "1" && -f /root/.claude/current.env ]]; then
    source /root/.claude/current.env
    export CLAUDE_CODE_OAUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-}"
  fi
  set -u
fi
unset ANTHROPIC_API_KEY
unset ANTHROPIC_BASE_URL
unset ANTHROPIC_AUTH_TOKEN
unset ANTHROPIC_AUTH_TOKEN_2
exec /usr/bin/claude "$@"
WRAPPER_EOF

rc=0
for target in $TARGETS; do
    log "→ ${target}"

    if printf '%s\n' "$RUNNER_ENV" | timeout 30 ssh "${SSH_OPTS[@]}" "$target" \
        "umask 077; mkdir -p /root/scripts; \
         [ -f /root/scripts/runner.env ] && cp -a /root/scripts/runner.env /root/scripts/runner.env.bak_lease_${STAMP}; \
         cat > /root/scripts/runner.env && chmod 600 /root/scripts/runner.env && \
         sh -n /root/scripts/runner.env" 2>/dev/null; then
        log "  ok runner.env"
    else
        log "  FAIL runner.env"; rc=1; continue
    fi

    if printf '%s\n' "$CLAUDE_WRAPPER" | timeout 30 ssh "${SSH_OPTS[@]}" "$target" \
        "[ -f /usr/local/bin/claude ] && cp -a /usr/local/bin/claude /usr/local/bin/claude.bak_lease_${STAMP}; \
         cat > /usr/local/bin/claude.new && chmod 755 /usr/local/bin/claude.new && \
         bash -n /usr/local/bin/claude.new && mv -f /usr/local/bin/claude.new /usr/local/bin/claude" 2>/dev/null; then
        log "  ok claude wrapper"
    else
        # 구문검사를 통과하지 못하면 교체하지 않는다 — 깨진 래퍼는 서버의 claude 를 통째로 죽인다
        log "  FAIL claude wrapper (기존 래퍼 유지)"; rc=1
    fi
done

exit "$rc"
