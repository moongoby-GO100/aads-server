#!/bin/bash
# Agent SDK용 번들 CLI 래퍼 — OAuth 직접 Anthropic 인증
# 충돌하는 환경변수 제거 후 CLAUDE_CODE_OAUTH_TOKEN만 사용
unset ANTHROPIC_API_KEY
unset ANTHROPIC_BASE_URL
unset ANTHROPIC_AUTH_TOKEN_2

# 슬롯 credential 모드에서는 HOME/.claude/.credentials.json의 accessToken과
# refreshToken을 CLI가 직접 관리한다. 고정 env access token은 이 파일이 없는
# 레거시 경로에서만 사용한다.
if [[ "${CLAUDE_SLOT_CREDENTIAL_MODE:-}" == "1" ]]; then
    unset CLAUDE_CODE_OAUTH_TOKEN
    unset ANTHROPIC_AUTH_TOKEN
    export HOME="${HOME:-/tmp/.claude-sdk}"
else
    # 릴레이가 선택한 env fallback 토큰이 1순위. 비어있을 때만 컨테이너의
    # ANTHROPIC_AUTH_TOKEN을 사용한다.
    export CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-$ANTHROPIC_AUTH_TOKEN}"
    unset ANTHROPIC_AUTH_TOKEN
    export HOME=/tmp/.claude-sdk
fi

# settings.json 격리
mkdir -p $HOME/.claude 2>/dev/null
[ -f $HOME/.claude/settings.json ] || echo "{}" > $HOME/.claude/settings.json

exec /usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude "$@"
