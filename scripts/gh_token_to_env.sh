#!/bin/bash
# GitHub 디바이스 플로우로 받은 토큰을 .env 의 GH_TOKEN 으로 넣는다.
#
# 사용: gh_token_to_env.sh <device_code>
#   device_code 는 아래로 미리 받아둔다(사람은 user_code 만 브라우저에 입력).
#     curl -s -X POST https://github.com/login/device/code \
#          -H "Accept: application/json" -d "client_id=178c6fc778ccc68e1d6a&scope="
#
# 왜 필요한가. gh 는 인증 결과를 ~/.config/gh/hosts.yml 에 두는데, 컨테이너의
# 파이썬 코드와 MCP 도구는 .env 를 본다. 두 곳이 갈리면 "gh 는 되는데 앱은
# 60회/시" 같은 상태가 된다. 그래서 양쪽에 같은 토큰을 넣는다.
#
# 규칙 셋.
#   - 토큰 값을 출력하지 않는다. 길이와 md5 앞 8자리까지만 로그에 남긴다.
#   - .env 에 append 하지 않는다. 기존 GH_TOKEN 줄을 지우고 다시 쓴다.
#     (error_book: config.env-duplicate-keys — append 가 중복 키를 만든다)
#   - 최대 15분만 기다린다(R-BG: 시간 상한 없는 백그라운드 금지).
#     디바이스 코드 자체가 약 15분 뒤 만료되므로 그 이상 기다릴 이유가 없다.

set -u
umask 077

CLIENT_ID="178c6fc778ccc68e1d6a"   # GitHub CLI 공개 OAuth 앱 (비밀값 아님)
DEVICE_CODE="${1:-}"
ENV_FILE="/root/aads/aads-server/.env"
LOG="/tmp/gh_token_to_env.log"

log() { echo "[$(date '+%F %T')] $1" >> "$LOG"; }

if [ -z "$DEVICE_CODE" ]; then
    log "device_code 인자 없음 — 중단"
    exit 2
fi

log "디바이스 인증 대기 시작 (최대 15분)"

for _ in $(seq 1 175); do
    RESP=$(curl -s -X POST https://github.com/login/oauth/access_token \
        -H "Accept: application/json" \
        -d "client_id=${CLIENT_ID}" \
        -d "device_code=${DEVICE_CODE}" \
        -d "grant_type=urn:ietf:params:oauth:grant-type:device_code" \
        --max-time 10)

    TOKEN=$(printf '%s' "$RESP" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
    ERR=$(printf '%s' "$RESP" | grep -o '"error":"[^"]*"' | cut -d'"' -f4)

    if [ -n "$TOKEN" ]; then
        cp -p "$ENV_FILE" "${ENV_FILE}.bak_ghtoken_$(date +%Y%m%d_%H%M%S)"

        # 파일이 개행으로 끝나지 않으면 새 키가 앞 줄에 붙는다
        [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >> "$ENV_FILE"

        if grep -q '^GH_TOKEN=' "$ENV_FILE"; then
            grep -v '^GH_TOKEN=' "$ENV_FILE" > "${ENV_FILE}.tmp"
            mv "${ENV_FILE}.tmp" "$ENV_FILE"
            log "기존 GH_TOKEN 줄 제거 후 재작성"
        fi

        printf 'GH_TOKEN=%s\n' "$TOKEN" >> "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        log "GH_TOKEN 등록 완료 (len=${#TOKEN}, md5=$(printf '%s' "$TOKEN" | md5sum | cut -c1-8))"

        # gh CLI 에도 같은 토큰을 넣는다(양쪽이 갈리지 않게)
        if printf '%s' "$TOKEN" | gh auth login --hostname github.com --with-token >/dev/null 2>&1; then
            log "gh CLI 인증 저장 완료"
        else
            log "gh CLI 저장 실패 — .env 의 GH_TOKEN 만 유효"
        fi

        LOGIN=$(curl -s -H "Authorization: token ${TOKEN}" --max-time 10 https://api.github.com/user | grep -o '"login":"[^"]*"' | head -1 | cut -d'"' -f4)
        LIMIT=$(curl -s -H "Authorization: token ${TOKEN}" --max-time 10 https://api.github.com/rate_limit | grep -o '"limit":[0-9]*' | head -1 | cut -d: -f2)
        log "검증 — 계정=${LOGIN:-조회실패} rate_limit=${LIMIT:-조회실패}/시"

        /root/aads/aads-server/scripts/check_env_duplicates.sh >> "$LOG" 2>&1
        exit 0
    fi

    case "$ERR" in
        authorization_pending) ;;
        slow_down) sleep 5 ;;
        expired_token|access_denied|incorrect_device_code)
            log "중단: $ERR"
            exit 1
            ;;
    esac

    sleep 5
done

log "15분 내 인증 없음 — 종료. 디바이스 코드를 새로 받아 다시 실행하라."
exit 1
