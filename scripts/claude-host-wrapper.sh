#!/bin/bash
set -euo pipefail

CONTAINER_NAME="${CLAUDE_DOCKER_CONTAINER:-aads-server}"
NONINTERACTIVE_WRAPPER="${CLAUDE_NONINTERACTIVE_WRAPPER:-/root/aads/aads-server/scripts/claude-docker-wrapper.sh}"
DIRECT_BIN="${CLAUDE_DIRECT_BIN:-/usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude}"

# 대화형 터미널에서는 컨테이너 내부 Claude REPL을 직접 띄운다.
# 이렇게 해야 호스트 GLIBC 제약을 피하면서 /login 인증도 가능하다.
if [[ -t 0 && -t 1 ]]; then
    exec docker exec -it \
        -e CLAUDE_CODE_OAUTH_TOKEN= \
        -e ANTHROPIC_AUTH_TOKEN= \
        -e ANTHROPIC_AUTH_TOKEN_2= \
        -e ANTHROPIC_API_KEY= \
        "$CONTAINER_NAME" \
        "$DIRECT_BIN" "$@"
fi

exec "$NONINTERACTIVE_WRAPPER" "$@"
