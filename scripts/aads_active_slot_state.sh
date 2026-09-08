#!/usr/bin/env bash
set -euo pipefail

# Single audited writer/guard for the AADS API blue-green routing markers.
# nginx is authoritative. Marker updates are accepted only when the requested
# slot is the one currently routed by nginx and the matching container exists.

STATE_DIR="${AADS_DEPLOY_STATE_DIR:-/root/aads/aads-server}"
UPSTREAM_CONF="${AADS_UPSTREAM_CONF:-/etc/nginx/conf.d/aads-upstream.conf}"
ACTIVE_PORT_FILE="${AADS_ACTIVE_PORT_FILE:-${STATE_DIR}/.active_port}"
ACTIVE_CONTAINER_FILE="${AADS_ACTIVE_CONTAINER_FILE:-${STATE_DIR}/.active_container}"
AUTH_FILE="${AADS_ACTIVE_SLOT_AUTH_FILE:-${STATE_DIR}/.active_slot_authorization}"
LOCK_FILE="${AADS_NGINX_LOCK:-/tmp/aads-nginx-upstream.lock}"
AUDIT_LOG="${AADS_CONTROL_AUDIT_LOG:-/var/log/aads-control-audit.jsonl}"

usage() {
    echo "usage: $0 write <8100|8102> <container> <actor> [detail] | check [actor] [--repair]" >&2
    exit 2
}

json_clean() {
    local value="${1:-}"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/ }"
    printf '%s' "$value"
}

audit() {
    local actor action result detail
    actor="$(json_clean "${1:-unknown}")"
    action="$(json_clean "${2:-active-slot-marker}")"
    result="$(json_clean "${3:-unknown}")"
    detail="$(json_clean "${4:-}")"
    mkdir -p "$(dirname "$AUDIT_LOG")" 2>/dev/null || true
    printf '{"ts":"%s","actor":"%s","uid":%s,"pid":%s,"ppid":%s,"action":"%s","target":"%s,%s","result":"%s","detail":"%s"}\n' \
        "$(date --iso-8601=seconds)" "$actor" "$(id -u)" "$$" "$PPID" "$action" \
        "$ACTIVE_PORT_FILE" "$ACTIVE_CONTAINER_FILE" "$result" "$detail" \
        >> "$AUDIT_LOG" 2>/dev/null || true
}

container_for_port() {
    case "${1:-}" in
        8100) echo "aads-server" ;;
        8102) echo "aads-server-green" ;;
        *) return 1 ;;
    esac
}

nginx_active_port() {
    local ports
    [[ -r "$UPSTREAM_CONF" ]] || return 1
    ports="$(awk '
        /upstream aads_api \{/ { in_api=1; next }
        in_api && /^}/ { in_api=0 }
        in_api && /server 127\.0\.0\.1:(8100|8102)/ && $0 !~ /backup/ {
            if (match($0, /127\.0\.0\.1:(8100|8102)/)) print substr($0, RSTART+10, RLENGTH-10)
        }
    ' "$UPSTREAM_CONF" | sort -u)"
    [[ "$(wc -w <<< "$ports" | tr -d ' ')" == "1" ]] || return 1
    printf '%s\n' "$ports"
}

marker_fingerprint() {
    [[ -f "$ACTIVE_PORT_FILE" && -f "$ACTIVE_CONTAINER_FILE" ]] || return 1
    stat -c '%i|%s|%y' "$ACTIVE_PORT_FILE" "$ACTIVE_CONTAINER_FILE" 2>/dev/null \
        | sha256sum | awk '{print $1}'
}

authorized_fingerprint() {
    [[ -r "$AUTH_FILE" ]] || return 1
    awk -F= '$1 == "fingerprint" {print $2; exit}' "$AUTH_FILE"
}

atomic_write() {
    local target="$1"
    local value="$2"
    local temp
    mkdir -p "$(dirname "$target")"
    temp="$(mktemp "${target}.tmp.XXXXXX")"
    printf '%s\n' "$value" > "$temp"
    chmod 0644 "$temp"
    mv -f "$temp" "$target"
}

write_authorization() {
    local port="$1"
    local container="$2"
    local actor="$3"
    local fingerprint temp
    fingerprint="$(marker_fingerprint)"
    temp="$(mktemp "${AUTH_FILE}.tmp.XXXXXX")"
    {
        printf 'fingerprint=%s\n' "$fingerprint"
        printf 'port=%s\n' "$port"
        printf 'container=%s\n' "$container"
        printf 'actor=%s\n' "$actor"
        printf 'written_at=%s\n' "$(date --iso-8601=seconds)"
        printf 'writer_pid=%s\n' "$$"
        printf 'writer_ppid=%s\n' "$PPID"
    } > "$temp"
    chmod 0644 "$temp"
    mv -f "$temp" "$AUTH_FILE"
}

lock_if_needed() {
    if [[ "${AADS_SLOT_STATE_LOCK_HELD:-false}" == "true" ]]; then
        return 0
    fi
    exec 9>"$LOCK_FILE"
    flock -w "${AADS_SLOT_STATE_LOCK_WAIT_SECONDS:-10}" 9
}

write_state() {
    local port="$1"
    local container="$2"
    local actor="$3"
    local detail="${4:-}"
    local expected routed old_port old_container fingerprint authorized
    expected="$(container_for_port "$port")" || {
        audit "$actor" "active-slot-write" "blocked" "invalid_port=${port}; ${detail}"
        return 1
    }
    if [[ "$container" != "$expected" ]]; then
        audit "$actor" "active-slot-write" "blocked" "port_container_mismatch=${port}/${container}; expected=${expected}; ${detail}"
        return 1
    fi
    if command -v docker >/dev/null 2>&1 \
        && [[ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" != "true" ]]; then
        audit "$actor" "active-slot-write" "blocked" "container_not_running=${container}; ${detail}"
        return 1
    fi
    lock_if_needed || {
        audit "$actor" "active-slot-write" "blocked" "nginx_lock_timeout; ${detail}"
        return 1
    }
    routed="$(nginx_active_port || true)"
    if [[ "$routed" != "$port" ]]; then
        audit "$actor" "active-slot-write" "blocked" "nginx=${routed:-ambiguous}; requested=${port}; ${detail}"
        return 1
    fi
    old_port="$(tr -d '[:space:]' < "$ACTIVE_PORT_FILE" 2>/dev/null || true)"
    old_container="$(tr -d '[:space:]' < "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true)"
    fingerprint="$(marker_fingerprint || true)"
    authorized="$(authorized_fingerprint || true)"
    if [[ "$old_port" == "$port" && "$old_container" == "$container" && -n "$fingerprint" && "$fingerprint" == "$authorized" ]]; then
        return 0
    fi
    atomic_write "$ACTIVE_PORT_FILE" "$port"
    atomic_write "$ACTIVE_CONTAINER_FILE" "$container"
    write_authorization "$port" "$container" "$actor"
    if [[ "$old_port" == "$port" && "$old_container" == "$container" && -n "$authorized" && "$fingerprint" != "$authorized" ]]; then
        audit "$actor" "active-slot-write" "repaired_unauthorized_mutation" "state=${port}/${container}; previous_fingerprint=${authorized}; observed_fingerprint=${fingerprint}; ${detail}"
    else
        audit "$actor" "active-slot-write" "success" "old=${old_port:-missing}/${old_container:-missing}; new=${port}/${container}; ${detail}"
    fi
}

check_state() {
    local actor="${1:-active-slot-guard}"
    local repair="${2:-false}"
    local routed expected marker_port marker_container fingerprint authorized reasons=""
    routed="$(nginx_active_port || true)"
    if [[ "$routed" != "8100" && "$routed" != "8102" ]]; then
        audit "$actor" "active-slot-check" "failed" "nginx_active_port_ambiguous"
        return 1
    fi
    expected="$(container_for_port "$routed")"
    marker_port="$(tr -d '[:space:]' < "$ACTIVE_PORT_FILE" 2>/dev/null || true)"
    marker_container="$(tr -d '[:space:]' < "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true)"
    fingerprint="$(marker_fingerprint || true)"
    authorized="$(authorized_fingerprint || true)"
    [[ "$marker_port" == "$routed" ]] || reasons="${reasons} port_marker=${marker_port:-missing} nginx=${routed};"
    [[ "$marker_container" == "$expected" ]] || reasons="${reasons} container_marker=${marker_container:-missing} expected=${expected};"
    [[ -n "$fingerprint" && "$fingerprint" == "$authorized" ]] || reasons="${reasons} unauthorized_or_untracked_write fingerprint=${fingerprint:-missing} authorized=${authorized:-missing};"
    if [[ -z "$reasons" ]]; then
        return 0
    fi
    audit "$actor" "active-slot-check" "warning" "$reasons"
    if [[ "$repair" == "true" ]]; then
        write_state "$routed" "$expected" "$actor" "guard_repair; ${reasons}"
        return $?
    fi
    return 1
}

case "${1:-}" in
    write)
        [[ $# -ge 4 ]] || usage
        write_state "$2" "$3" "$4" "${5:-}"
        ;;
    check)
        actor="${2:-active-slot-guard}"
        repair=false
        [[ "${3:-}" == "--repair" ]] && repair=true
        check_state "$actor" "$repair"
        ;;
    *) usage ;;
esac
