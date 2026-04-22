#!/bin/bash
# Claude Code CLI + Codex CLI 전 서버 자동 업데이트
# 크론: 매일 04:00 KST

set -euo pipefail

[[ -f /root/aads/.env ]] && source /root/aads/.env

LOG="/var/log/claude_update.log"
TELEGRAM_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
SSH_COMMON_OPTS=(-o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no)

declare -a SUCCESS_SERVERS=()
declare -a FAILED_SERVERS=()

mkdir -p "$(dirname "$LOG")"

send_telegram() {
    local msg="$1"
    if [[ -n "$TELEGRAM_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${msg}" \
            -d "parse_mode=HTML" > /dev/null 2>&1 || true
    fi
}

log() {
    printf '[%s] %s\n' "$(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST')" "$*" | tee -a "$LOG"
}

normalize_version() {
    local value="${1:-}"
    if [[ -n "$value" ]]; then
        printf '%s\n' "$value"
    else
        printf 'unavailable\n'
    fi
}

unavailable_versions() {
    printf 'claude=unavailable\ncodex=unavailable\nsdk=unavailable\n'
}

capture_local_versions() {
    local claude_version codex_version sdk_version

    claude_version="$(claude --version 2>/dev/null || true)"
    codex_version="$(codex --version 2>/dev/null || true)"
    sdk_version="$(
        { python3.11 -m pip show claude-agent-sdk 2>/dev/null || pip3 show claude-agent-sdk 2>/dev/null || true; } |
            awk '/^Version: / { print $2; exit }'
    )"

    printf 'claude=%s\n' "$(normalize_version "$claude_version")"
    printf 'codex=%s\n' "$(normalize_version "$codex_version")"
    printf 'sdk=%s\n' "$(normalize_version "$sdk_version")"
}

capture_remote_versions() {
    local host="$1"
    local port="${2:-22}"

    ssh "${SSH_COMMON_OPTS[@]}" -p "$port" "$host" 'bash -s' <<'EOF'
claude_version="$(claude --version 2>/dev/null || true)"
codex_version="$(codex --version 2>/dev/null || true)"
sdk_version="$(
    { python3.11 -m pip show claude-agent-sdk 2>/dev/null || pip3 show claude-agent-sdk 2>/dev/null || true; } |
        awk '/^Version: / { print $2; exit }'
)"

[[ -n "$claude_version" ]] || claude_version="unavailable"
[[ -n "$codex_version" ]] || codex_version="unavailable"
[[ -n "$sdk_version" ]] || sdk_version="unavailable"

printf 'claude=%s\n' "$claude_version"
printf 'codex=%s\n' "$codex_version"
printf 'sdk=%s\n' "$sdk_version"
EOF
}

version_value() {
    local versions="$1"
    local key="$2"

    printf '%s\n' "$versions" | sed -n "s/^${key}=//p" | head -n 1
}

run_local_update() {
    mkdir -p /root/tmp
    npm update -g @anthropic-ai/claude-code @openai/codex >> "$LOG" 2>&1
    TMPDIR=/root/tmp bash -lc '
        if command -v python3.11 >/dev/null 2>&1; then
            python3.11 -m pip install --upgrade claude-agent-sdk ||
                pip3 install --break-system-packages --upgrade claude-agent-sdk
        else
            pip3 install --break-system-packages --upgrade claude-agent-sdk
        fi
    ' >> "$LOG" 2>&1
}

run_remote_update() {
    local host="$1"
    local port="${2:-22}"

    ssh "${SSH_COMMON_OPTS[@]}" -p "$port" "$host" 'bash -s' >> "$LOG" 2>&1 <<'EOF'
set -euo pipefail

mkdir -p /root/tmp
npm update -g @anthropic-ai/claude-code @openai/codex
TMPDIR=/root/tmp bash -lc '
    if command -v python3.11 >/dev/null 2>&1; then
        python3.11 -m pip install --upgrade claude-agent-sdk ||
            pip3 install --break-system-packages --upgrade claude-agent-sdk
    else
        pip3 install --break-system-packages --upgrade claude-agent-sdk
    fi
'
EOF
}

log_versions() {
    local server="$1"
    local stage="$2"
    local versions="$3"

    log "[$server] ${stage} claude=$(version_value "$versions" "claude") codex=$(version_value "$versions" "codex") sdk=$(version_value "$versions" "sdk")"
}

notify_if_changed() {
    local server="$1"
    local before_versions="$2"
    local after_versions="$3"
    local before_claude after_claude before_codex after_codex before_sdk after_sdk
    local changes=""

    before_claude="$(version_value "$before_versions" "claude")"
    after_claude="$(version_value "$after_versions" "claude")"
    before_codex="$(version_value "$before_versions" "codex")"
    after_codex="$(version_value "$after_versions" "codex")"
    before_sdk="$(version_value "$before_versions" "sdk")"
    after_sdk="$(version_value "$after_versions" "sdk")"

    if [[ "$before_claude" != "$after_claude" ]]; then
        changes+="Claude: ${before_claude} -> ${after_claude}"$'\n'
    fi
    if [[ "$before_codex" != "$after_codex" ]]; then
        changes+="Codex: ${before_codex} -> ${after_codex}"$'\n'
    fi
    if [[ "$before_sdk" != "$after_sdk" ]]; then
        changes+="SDK: ${before_sdk} -> ${after_sdk}"$'\n'
    fi

    if [[ -n "$changes" ]]; then
        send_telegram "<b>${server} update applied</b>
${changes}"
    fi
}

update_local_server() {
    local server="$1"
    local before_versions after_versions

    log "[$server] update started"
    before_versions="$(capture_local_versions)"
    log_versions "$server" "before" "$before_versions"

    if run_local_update; then
        after_versions="$(capture_local_versions)"
        log_versions "$server" "after " "$after_versions"
        notify_if_changed "$server" "$before_versions" "$after_versions"
        SUCCESS_SERVERS+=("$server")
        log "[$server] update finished"
        return 0
    fi

    FAILED_SERVERS+=("$server")
    log "[$server] update failed"
    return 1
}

update_remote_server() {
    local server="$1"
    local host="$2"
    local port="$3"
    local before_versions after_versions

    log "[$server] update started"
    if ! before_versions="$(capture_remote_versions "$host" "$port")"; then
        before_versions="$(unavailable_versions)"
        log "[$server] before version check failed"
    fi
    log_versions "$server" "before" "$before_versions"

    if run_remote_update "$host" "$port"; then
        if ! after_versions="$(capture_remote_versions "$host" "$port")"; then
            after_versions="$(unavailable_versions)"
            log "[$server] after version check failed"
        fi
        log_versions "$server" "after " "$after_versions"
        notify_if_changed "$server" "$before_versions" "$after_versions"
        SUCCESS_SERVERS+=("$server")
        log "[$server] update finished"
        return 0
    fi

    FAILED_SERVERS+=("$server")
    log "[$server] update failed"
    return 1
}

join_by_comma() {
    local value
    local result=""

    for value in "$@"; do
        if [[ -n "$result" ]]; then
            result+=", "
        fi
        result+="$value"
    done

    printf '%s\n' "${result:-none}"
}

log "=== Claude/Codex update started ==="

update_remote_server "114" "114.207.244.86" "7916" || true
update_local_server "68" || true
update_remote_server "211" "211.188.51.113" "22" || true

summary_message="<b>Claude/Codex update summary</b>
Success: $(join_by_comma "${SUCCESS_SERVERS[@]}")
Failed: $(join_by_comma "${FAILED_SERVERS[@]}")"

log "summary success=$(join_by_comma "${SUCCESS_SERVERS[@]}") failed=$(join_by_comma "${FAILED_SERVERS[@]}")"
send_telegram "$summary_message"
log "=== Claude/Codex update completed ==="
