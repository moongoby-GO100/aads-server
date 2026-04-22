#!/bin/bash
# Agent SDK용 번들 CLI 래퍼 — OAuth 직접 Anthropic 인증
# 충돌하는 환경변수 제거 후 CLAUDE_CODE_OAUTH_TOKEN만 사용
unset ANTHROPIC_API_KEY
unset ANTHROPIC_BASE_URL
unset ANTHROPIC_AUTH_TOKEN_2

# 릴레이가 `docker exec -e CLAUDE_CODE_OAUTH_TOKEN=<slot>` 로 주입한 슬롯 토큰이 1순위.
# 비어있을 때만 컨테이너 .env 의 ANTHROPIC_AUTH_TOKEN(=slot1) 으로 폴백.
# (이전 버전은 ANTHROPIC_AUTH_TOKEN 을 우선 사용해 relay 가 선택한 slot2 토큰을 덮어써
#  모든 요청이 slot1 로만 흘러갔고, slot1 의 seven_day 한도 소진 후 슬롯 폴백이 무효화됨)
export CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-$ANTHROPIC_AUTH_TOKEN}"
unset ANTHROPIC_AUTH_TOKEN

# 호스트 settings.json 격리
export HOME=/tmp/.claude-sdk
mkdir -p $HOME/.claude 2>/dev/null
[ -f $HOME/.claude/settings.json ] || echo "{}" > $HOME/.claude/settings.json

exec /usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude "$@"
