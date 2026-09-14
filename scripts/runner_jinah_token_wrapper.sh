#!/usr/bin/env bash
# jinah244 러너 진입점 — 진아 계정 토큰만 주입하고 러너 본체를 exec 한다.
#
# 러너 본체는 ANTHROPIC_AUTH_TOKEN 이 비면 token_missing 으로 즉시 죽는다.
# 244 는 CEO 지시로 AADS 릴레이 슬롯 lease 를 받지 않으므로 그 값을
# /etc/biseo.env(0600 root)의 CLAUDE_CODE_OAUTH_TOKEN 에서 매 기동마다 읽는다.
#
# 사본을 따로 만들지 않는 이유: 진아 측이 `claude setup-token` 을 재발급하면
# 사본은 조용히 낡는다. 원본을 그때그때 읽으면 재기동만으로 따라간다.
# biseo.env 의 나머지 변수는 서브셸에 가둬 러너 환경으로 새지 않게 한다.
set -uo pipefail
BISEO_ENV="${BISEO_ENV:-/etc/biseo.env}"

ANTHROPIC_AUTH_TOKEN="$(
    set +u
    # shellcheck disable=SC1090
    . "$BISEO_ENV" >/dev/null 2>&1
    printf '%s' "${CLAUDE_CODE_OAUTH_TOKEN:-}"
)"
export ANTHROPIC_AUTH_TOKEN

if [[ -z "$ANTHROPIC_AUTH_TOKEN" ]]; then
    echo "[runner-jinah] $BISEO_ENV 에서 CLAUDE_CODE_OAUTH_TOKEN 을 찾지 못했다" >&2
    exit 78
fi

unset ANTHROPIC_API_KEY
exec /root/scripts/pipeline-runner.sh "$@"
