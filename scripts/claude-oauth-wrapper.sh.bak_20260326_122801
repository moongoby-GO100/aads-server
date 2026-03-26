#!/bin/bash
# Agent SDK용 번들 CLI 래퍼 — OAuth 직접 Anthropic 인증
# 충돌하는 환경변수 제거 후 CLAUDE_CODE_OAUTH_TOKEN만 사용
unset ANTHROPIC_API_KEY
unset ANTHROPIC_BASE_URL
unset ANTHROPIC_AUTH_TOKEN_2

# ANTHROPIC_AUTH_TOKEN → CLAUDE_CODE_OAUTH_TOKEN으로 전달
export CLAUDE_CODE_OAUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-$CLAUDE_CODE_OAUTH_TOKEN}"
unset ANTHROPIC_AUTH_TOKEN

# 호스트 settings.json 격리
export HOME=/tmp/.claude-sdk
mkdir -p $HOME/.claude 2>/dev/null
[ -f $HOME/.claude/settings.json ] || echo "{}" > $HOME/.claude/settings.json

exec /usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude "$@"
