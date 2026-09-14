#!/bin/bash
# claude-lease — 원격 서버(contabo14 / cafe24_114) 터미널에서 사람이 직접 쓰는 진입점.
# /usr/local/bin/claude-lease 로 배치된다.
#
#   claude-lease -p "무엇을 하라"        # 단발 실행
#   claude-lease                          # 대화형
#
# contabo116 이 밀어 넣은 임시 출입증(accessToken)을 격리 HOME 에 staging 한 뒤 CLI 를 띄운다.
# 출입증이 만료됐으면 78 로 죽는다 — 그때는 contabo116 에서 claude-lease-push.sh 를 돌린다.
set -euo pipefail

WRAPPER="${CLAUDE_LEASE_WRAPPER:-/root/scripts/claude-lease-wrapper.sh}"
CLI="${CLAUDE_LEASE_CLI:-}"

if [[ -z "$CLI" ]]; then
    # 번들 CLI(2.1.259) 를 최우선으로 쓴다. 배포판 CLI(2.1.183/2.1.270) 는 같은 계정·같은
    # 토큰으로도 "weekly limit" 을 돌려주는 사례가 2026-09-14 실측으로 확인됐다.
    for candidate in /root/vendor-claude-cli/claude /root/aads/vendor/claude-cli/claude /usr/local/bin/claude claude; do
        if command -v "$candidate" >/dev/null 2>&1; then
            CLI="$candidate"
            break
        fi
    done
fi
[[ -n "$CLI" ]] || { echo "claude CLI 를 찾지 못했다" >&2; exit 127; }
[[ -x "$WRAPPER" ]] || { echo "lease wrapper 가 없다: ${WRAPPER}" >&2; exit 78; }

exec "$WRAPPER" "$CLI" "$@"
