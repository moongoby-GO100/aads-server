#!/usr/bin/env bash
set -euo pipefail

# Host-level AADS API watchdog. This must run outside the application containers.
# It never restarts a running API process. On two consecutive failures it first
# fails over to the healthy standby; only a Supervisor STOPPED process may be
# started in-place when no healthy standby exists.

COMPOSE_DIR="${AADS_COMPOSE_DIR:-/root/aads/aads-server}"
UPSTREAM_CONF="${AADS_UPSTREAM_CONF:-/etc/nginx/conf.d/aads-upstream.conf}"
ACTIVE_PORT_FILE="${AADS_ACTIVE_PORT_FILE:-${COMPOSE_DIR}/.active_port}"
ACTIVE_CONTAINER_FILE="${AADS_ACTIVE_CONTAINER_FILE:-${COMPOSE_DIR}/.active_container}"
STATE_DIR="${AADS_WATCHDOG_STATE_DIR:-/run/aads-api-watchdog}"
FAIL_FILE="${STATE_DIR}/consecutive_failures"
LOCK_FILE="${STATE_DIR}/watchdog.lock"
DEPLOY_LOCK="${AADS_DEPLOY_LOCK:-/tmp/aads-deploy.lock}"
NGINX_LOCK="${AADS_NGINX_LOCK:-/tmp/aads-nginx-upstream.lock}"
AUDIT_LOG="${AADS_CONTROL_AUDIT_LOG:-/var/log/aads-control-audit.jsonl}"
FAIL_THRESHOLD="${AADS_WATCHDOG_FAIL_THRESHOLD:-2}"
DRY_RUN=false

if [[ "${1:-}" == "--dry-run" || "${1:-}" == "--check" ]]; then
    DRY_RUN=true
elif [[ -n "${1:-}" ]]; then
    echo "usage: $0 [--dry-run|--check]" >&2
    exit 2
fi

mkdir -p "$STATE_DIR"
exec 7>"$LOCK_FILE"
if ! flock -n 7; then
    exit 0
fi

log() {
    logger -t aads-api-watchdog -- "$*"
    echo "[$(date '+%Y-%m-%d %H:%M:%S%z')] $*"
}

json_clean() {
    local value="${1:-}"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/ }"
    printf '%s' "$value"
}

audit() {
    local action="$(json_clean "${1:-unknown}")"
    local target="$(json_clean "${2:-unknown}")"
    local result="$(json_clean "${3:-unknown}")"
    local detail="$(json_clean "${4:-}")"
    printf '{"ts":"%s","actor":"host-watchdog","action":"%s","target":"%s","result":"%s","detail":"%s"}\n' \
        "$(date --iso-8601=seconds)" "$action" "$target" "$result" "$detail" >> "$AUDIT_LOG"
}

container_for_port() {
    case "$1" in
        8100) echo "aads-server" ;;
        8102) echo "aads-server-green" ;;
        *) return 1 ;;
    esac
}

peer_port_for() {
    case "$1" in
        8100) echo "8102" ;;
        8102) echo "8100" ;;
        *) return 1 ;;
    esac
}

nginx_active_port() {
    local ports
    ports="$(awk '
        /upstream aads_api \{/ { in_api=1; next }
        in_api && /^}/ { in_api=0 }
        in_api && /server 127\.0\.0\.1:(8100|8102)/ && $0 !~ /backup/ {
            if (match($0, /127\.0\.0\.1:(8100|8102)/)) print substr($0, RSTART+10, RLENGTH-10)
        }
    ' "$UPSTREAM_CONF" | sort -u)"
    if [[ "$(wc -w <<< "$ports" | tr -d ' ')" != "1" ]]; then
        return 1
    fi
    printf '%s\n' "$ports"
}

supervisor_state() {
    local container="$1"
    docker exec "$container" supervisorctl status aads-api 2>/dev/null | awk 'NR==1 {print $2}'
}

slot_detail() {
    local port="$1"
    local container="$2"
    local running health supervisor live api
    running="$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || echo false)"
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || echo missing)"
    supervisor="$(supervisor_state "$container" || true)"
    live="$(curl -fsS --max-time 4 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/health/live" 2>/dev/null || true)"
    api="$(curl -fsS --max-time 1 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/api/v1/health" 2>/dev/null || true)"
    live="${live:-000}"
    api="${api:-000}"
    printf 'running=%s health=%s supervisor=%s live=%s api=%s' "$running" "$health" "${supervisor:-unknown}" "$live" "$api"
}

slot_healthy() {
    local port="$1"
    local container="$2"
    [[ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" == "true" ]] || return 1
    [[ "$(supervisor_state "$container" || true)" == "RUNNING" ]] || return 1
    curl -fsS --max-time 4 "http://127.0.0.1:${port}/health/live" >/dev/null 2>&1 || return 1
}

nginx_test() {
    if command -v nginx >/dev/null 2>&1; then
        nginx -t
    else
        docker exec aads-nginx nginx -t
    fi
}

nginx_reload() {
    if command -v nginx >/dev/null 2>&1 && systemctl is-active --quiet nginx; then
        systemctl reload nginx
    else
        docker exec aads-nginx nginx -s reload
    fi
}

deploy_in_progress() {
    local deploy_pid=""
    [[ -f "$DEPLOY_LOCK" ]] || return 1
    deploy_pid="$(tr -cd '0-9' < "$DEPLOY_LOCK" 2>/dev/null || true)"
    [[ -n "$deploy_pid" ]] && kill -0 "$deploy_pid" 2>/dev/null
}

switch_to_peer() {
    local old_port="$1"
    local new_port="$2"
    local old_container="$3"
    local new_container="$4"
    local temp_conf backup_conf

    if $DRY_RUN; then
        log "DRY-RUN failover ${old_container}:${old_port} -> ${new_container}:${new_port}"
        return 0
    fi

    exec 9>"$NGINX_LOCK"
    if ! flock -w 10 9; then
        audit "failover" "${new_container}:${new_port}" "blocked" "nginx lock busy"
        return 1
    fi

    # Re-check after acquiring the shared deploy/nginx lock.
    if deploy_in_progress || ! slot_healthy "$new_port" "$new_container"; then
        audit "failover" "${new_container}:${new_port}" "blocked" "deployment active or peer became unhealthy"
        return 1
    fi

    temp_conf="$(mktemp "${UPSTREAM_CONF}.watchdog.XXXXXX")"
    backup_conf="${UPSTREAM_CONF}.watchdog.previous"
    cp -p "$UPSTREAM_CONF" "$backup_conf"
    sed -E \
        -e "s/server 127\\.0\\.0\\.1:${new_port} [^;]*;/server 127.0.0.1:${new_port} max_fails=1 fail_timeout=10s;/g" \
        -e "s/server 127\\.0\\.0\\.1:${old_port} [^;]*;/server 127.0.0.1:${old_port} max_fails=1 fail_timeout=10s backup;/g" \
        "$UPSTREAM_CONF" > "$temp_conf"
    chmod --reference="$UPSTREAM_CONF" "$temp_conf"
    chown --reference="$UPSTREAM_CONF" "$temp_conf"
    mv "$temp_conf" "$UPSTREAM_CONF"

    if ! nginx_test >/dev/null 2>&1; then
        cp -p "$backup_conf" "$UPSTREAM_CONF"
        audit "failover" "${new_container}:${new_port}" "failed" "nginx configuration test failed; rolled back"
        return 1
    fi
    if ! nginx_reload; then
        cp -p "$backup_conf" "$UPSTREAM_CONF"
        nginx_test >/dev/null 2>&1 && nginx_reload >/dev/null 2>&1 || true
        audit "failover" "${new_container}:${new_port}" "failed" "nginx reload failed; rolled back"
        return 1
    fi

    printf '%s\n' "$new_port" > "$ACTIVE_PORT_FILE"
    printf '%s\n' "$new_container" > "$ACTIVE_CONTAINER_FILE"
    docker exec "$new_container" sh -c 'printf true > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    audit "failover" "${old_container}:${old_port}->${new_container}:${new_port}" "success" "two consecutive active-slot failures"
    log "FAILOVER complete ${old_container}:${old_port} -> ${new_container}:${new_port}"
}

active_port="$(nginx_active_port || true)"
if [[ "$active_port" != "8100" && "$active_port" != "8102" ]]; then
    audit "check" "nginx-upstream" "failed" "active port is ambiguous"
    log "ERROR active nginx upstream is ambiguous; no mutation performed"
    exit 1
fi
active_container="$(container_for_port "$active_port")"
peer_port="$(peer_port_for "$active_port")"
peer_container="$(container_for_port "$peer_port")"

if slot_healthy "$active_port" "$active_container"; then
    printf '0\n' > "$FAIL_FILE"
    # Keep state files aligned with the authoritative nginx upstream.
    if ! $DRY_RUN; then
        exec 9>"$NGINX_LOCK"
        if flock -w 2 9 && [[ "$(nginx_active_port || true)" == "$active_port" ]]; then
            printf '%s\n' "$active_port" > "$ACTIVE_PORT_FILE"
            printf '%s\n' "$active_container" > "$ACTIVE_CONTAINER_FILE"
        fi
    fi
    log "OK active=${active_container}:${active_port}"
    exit 0
fi

if deploy_in_progress; then
    audit "check" "${active_container}:${active_port}" "deferred" "deployment lock is active"
    log "WARN active unhealthy during deployment; recovery deferred"
    exit 0
fi

fail_count="0"
if [[ -f "$FAIL_FILE" ]]; then
    fail_count="$(tr -cd '0-9' < "$FAIL_FILE" 2>/dev/null || true)"
fi
fail_count="${fail_count:-0}"
fail_count=$((fail_count + 1))
printf '%s\n' "$fail_count" > "$FAIL_FILE"
active_detail="$(slot_detail "$active_port" "$active_container")"
audit "check" "${active_container}:${active_port}" "failed" "attempt=${fail_count}/${FAIL_THRESHOLD} ${active_detail}"
log "WARN active unhealthy attempt=${fail_count}/${FAIL_THRESHOLD} ${active_container}:${active_port} ${active_detail}"

if (( fail_count < FAIL_THRESHOLD )); then
    exit 0
fi

if slot_healthy "$peer_port" "$peer_container"; then
    switch_to_peer "$active_port" "$peer_port" "$active_container" "$peer_container"
    printf '0\n' > "$FAIL_FILE"
    exit 0
fi

active_supervisor="$(supervisor_state "$active_container" || true)"
if [[ "$active_supervisor" == "STOPPED" ]]; then
    if $DRY_RUN; then
        log "DRY-RUN start stopped child ${active_container}:aads-api"
        exit 0
    fi
    audit "supervisor-start" "${active_container}:aads-api" "started" "no healthy standby; child was STOPPED"
    docker exec "$active_container" supervisorctl start aads-api >/dev/null
    for _ in $(seq 1 10); do
        sleep 2
        if slot_healthy "$active_port" "$active_container"; then
            printf '0\n' > "$FAIL_FILE"
            audit "supervisor-start" "${active_container}:aads-api" "success" "health restored"
            log "RECOVERED stopped API child on ${active_container}:${active_port}"
            exit 0
        fi
    done
    audit "supervisor-start" "${active_container}:aads-api" "failed" "health not restored within 20 seconds"
fi

audit "recovery" "aads-api" "failed" "both slots unhealthy; no restart loop performed"
log "ERROR both API slots unhealthy; manual intervention required"
exit 1
