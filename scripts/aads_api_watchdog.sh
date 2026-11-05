#!/usr/bin/env bash
set -euo pipefail

# Host-level AADS API watchdog. This must run outside the application containers.
# It never restarts a running API process. It fails over to the healthy standby
# when either rule fires; only a Supervisor STOPPED process may be started
# in-place when no healthy standby exists.
#   hard failure: container not running, or supervisor state is not RUNNING.
#   soft failure: anything else (running + RUNNING but /health/live slow or
#                 non-200). Docker health=unhealthy is NOT used to classify:
#                 the container healthcheck hits the same /health/live, so it
#                 only echoes a soft failure.
#   rule hard_streak: FAIL_THRESHOLD consecutive hard failures (ignores cooldown).
#   rule soft_streak: SOFT_FAIL_THRESHOLD consecutive failures of any kind, and
#                     FAILOVER_COOLDOWN_SEC elapsed since the last failover.

COMPOSE_DIR="${AADS_COMPOSE_DIR:-/root/aads/aads-server}"
UPSTREAM_CONF="${AADS_UPSTREAM_CONF:-/etc/nginx/conf.d/aads-upstream.conf}"
ACTIVE_PORT_FILE="${AADS_ACTIVE_PORT_FILE:-${COMPOSE_DIR}/.active_port}"
ACTIVE_CONTAINER_FILE="${AADS_ACTIVE_CONTAINER_FILE:-${COMPOSE_DIR}/.active_container}"
ACTIVE_SLOT_STATE_WRITER="${AADS_ACTIVE_SLOT_STATE_WRITER:-${COMPOSE_DIR}/scripts/aads_active_slot_state.sh}"
STATE_DIR="${AADS_WATCHDOG_STATE_DIR:-/run/aads-api-watchdog}"
FAIL_FILE="${STATE_DIR}/consecutive_failures"
HARD_FAIL_FILE="${STATE_DIR}/consecutive_hard_failures"
LAST_FAILOVER_FILE="${STATE_DIR}/last_failover_epoch"
LOCK_FILE="${STATE_DIR}/watchdog.lock"
DEPLOY_LOCK="${AADS_DEPLOY_LOCK:-/tmp/aads-deploy.lock}"
NGINX_LOCK="${AADS_NGINX_LOCK:-/tmp/aads-nginx-upstream.lock}"
AUDIT_LOG="${AADS_CONTROL_AUDIT_LOG:-/var/log/aads-control-audit.jsonl}"
FAIL_THRESHOLD="${AADS_WATCHDOG_FAIL_THRESHOLD:-2}"
SOFT_FAIL_THRESHOLD="${AADS_WATCHDOG_SOFT_FAIL_THRESHOLD:-5}"
FAILOVER_COOLDOWN_SEC="${AADS_WATCHDOG_FAILOVER_COOLDOWN_SEC:-600}"
MEMORY_WARN_PCT="${AADS_WATCHDOG_MEMORY_WARN_PCT:-85}"
MEMORY_CLEAR_PCT="${AADS_WATCHDOG_MEMORY_CLEAR_PCT:-75}"
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

# 판정(slot_healthy)이 실제로 쓴 측정값. 판정 후 재측정(slot_detail)과 달라질 수 있어 따로 남긴다.
LAST_PROBE_DETAIL=""
ACTIVE_PROBE_DETAIL=""
PEER_PROBE_DETAIL=""

slot_healthy() {
    local port="$1"
    local container="$2"
    local running supervisor out rc=0 code="" secs=""
    running="$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)"
    if [[ "$running" != "true" ]]; then
        LAST_PROBE_DETAIL="probe_fail=running running=${running:-unknown}"
        return 1
    fi
    supervisor="$(supervisor_state "$container" || true)"
    if [[ "$supervisor" != "RUNNING" ]]; then
        LAST_PROBE_DETAIL="probe_fail=supervisor running=${running} supervisor=${supervisor:-unknown}"
        return 1
    fi
    out="$(curl -fsS --max-time 4 -o /dev/null -w '%{http_code} %{time_total}' "http://127.0.0.1:${port}/health/live" 2>/dev/null)" || rc=$?
    read -r code secs <<< "$out" || true
    if (( rc != 0 )); then
        LAST_PROBE_DETAIL="probe_fail=live live_code=${code:-000} live_s=${secs:-na} curl_rc=${rc} running=${running} supervisor=${supervisor}"
        return 1
    fi
    LAST_PROBE_DETAIL="probe_fail=none live_code=${code:-000} live_s=${secs:-na} curl_rc=0 running=${running} supervisor=${supervisor}"
}

read_counter() {
    local value=""
    [[ -f "$1" ]] && value="$(tr -cd '0-9' < "$1" 2>/dev/null || true)"
    echo "${value:-0}"
}

reset_fail_counters() {
    printf '0\n' > "$FAIL_FILE"
    printf '0\n' > "$HARD_FAIL_FILE"
}

# hard: container not running, or supervisor not RUNNING (STOPPED/FATAL/EXITED/
# BACKOFF/unknown). soft: everything else. Docker health is deliberately unused.
slot_failure_kind() {
    local container="$1"
    local running supervisor
    running="$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)"
    [[ "$running" == "true" ]] || { echo hard; return 0; }
    supervisor="$(supervisor_state "$container" || true)"
    [[ "$supervisor" == "RUNNING" ]] || { echo hard; return 0; }
    echo soft
}

failover_cooldown_remaining() {
    local last now elapsed
    last="$(read_counter "$LAST_FAILOVER_FILE")"
    if (( last == 0 )); then
        echo 0
        return 0
    fi
    now="$(date +%s)"
    elapsed=$((now - last))
    (( elapsed >= 0 )) || elapsed=0
    if (( elapsed >= FAILOVER_COOLDOWN_SEC )); then
        echo 0
    else
        echo $((FAILOVER_COOLDOWN_SEC - elapsed))
    fi
}

container_memory_percent() {
    local container="$1"
    docker stats --no-stream --format '{{.MemPerc}}' "$container" 2>/dev/null \
        | awk 'NR==1 {gsub(/%/, "", $1); print int($1 + 0.5)}'
}

audit_slot_memory() {
    local container="$1"
    local state_file="${STATE_DIR}/memory_${container}"
    local oom_file="${STATE_DIR}/oom_${container}"
    local mem_pct oom_killed

    mem_pct="$(container_memory_percent "$container" || true)"
    if [[ "$mem_pct" =~ ^[0-9]+$ ]]; then
        if (( mem_pct >= MEMORY_WARN_PCT )); then
            if [[ ! -f "$state_file" ]]; then
                audit "memory-pressure" "$container" "warning" "mem_pct=${mem_pct} threshold=${MEMORY_WARN_PCT}"
                log "WARN memory pressure ${container} mem_pct=${mem_pct}"
                printf '%s\n' "$mem_pct" > "$state_file"
            fi
        elif (( mem_pct <= MEMORY_CLEAR_PCT )); then
            if [[ -f "$state_file" ]]; then
                audit "memory-pressure" "$container" "cleared" "mem_pct=${mem_pct} clear_threshold=${MEMORY_CLEAR_PCT}"
                rm -f "$state_file"
            fi
        fi
    fi

    oom_killed="$(docker inspect -f '{{.State.OOMKilled}}' "$container" 2>/dev/null || echo false)"
    if [[ "$oom_killed" == "true" && ! -f "$oom_file" ]]; then
        audit "oom-observed" "$container" "warning" "docker State.OOMKilled=true"
        log "WARN oom observed ${container}"
        printf 'true\n' > "$oom_file"
    fi
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

write_active_slot_state() {
    local port="$1"
    local container="$2"
    local detail="${3:-}"
    if [[ ! -x "$ACTIVE_SLOT_STATE_WRITER" ]]; then
        audit "active-slot-write" "${container}:${port}" "failed" "state writer unavailable: ${ACTIVE_SLOT_STATE_WRITER}"
        return 1
    fi
    AADS_SLOT_STATE_LOCK_HELD=true "$ACTIVE_SLOT_STATE_WRITER" write \
        "$port" "$container" "host-watchdog" "$detail"
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
        audit "failover" "${new_container}:${new_port}" "blocked" "deployment active or peer became unhealthy peer_probe: ${LAST_PROBE_DETAIL}"
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

    if ! write_active_slot_state "$new_port" "$new_container" "watchdog failover after routed health"; then
        cp -p "$backup_conf" "$UPSTREAM_CONF"
        nginx_test >/dev/null 2>&1 && nginx_reload >/dev/null 2>&1 || true
        audit "failover" "${new_container}:${new_port}" "failed" "marker authorization failed; nginx rolled back"
        return 1
    fi
    docker exec "$new_container" sh -c 'printf true > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    printf '%s\n' "$(date +%s)" > "$LAST_FAILOVER_FILE"
    audit "failover" "${old_container}:${old_port}->${new_container}:${new_port}" "success" "consecutive active-slot failures ${decision_detail:-} active_probe: ${ACTIVE_PROBE_DETAIL} peer_probe: ${PEER_PROBE_DETAIL}"
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

audit_slot_memory "$active_container"
audit_slot_memory "$peer_container"

if slot_healthy "$active_port" "$active_container"; then
    ACTIVE_PROBE_DETAIL="$LAST_PROBE_DETAIL"
    reset_fail_counters
    # Keep state files aligned with the authoritative nginx upstream.
    if ! $DRY_RUN; then
        exec 9>"$NGINX_LOCK"
        if flock -w 2 9 && [[ "$(nginx_active_port || true)" == "$active_port" ]]; then
            if ! write_active_slot_state "$active_port" "$active_container" "healthy-route marker guard"; then
                audit "active-slot-check" "${active_container}:${active_port}" "warning" "marker repair failed"
                log "WARN active-slot marker authorization/repair failed"
            fi
        fi
    fi
    log "OK active=${active_container}:${active_port}"
    exit 0
fi

ACTIVE_PROBE_DETAIL="$LAST_PROBE_DETAIL"

if deploy_in_progress; then
    audit "check" "${active_container}:${active_port}" "deferred" "deployment lock is active"
    log "WARN active unhealthy during deployment; recovery deferred"
    exit 0
fi

fail_count=$(( $(read_counter "$FAIL_FILE") + 1 ))
printf '%s\n' "$fail_count" > "$FAIL_FILE"
fail_kind="$(slot_failure_kind "$active_container")"
if [[ "$fail_kind" == "hard" ]]; then
    hard_count=$(( $(read_counter "$HARD_FAIL_FILE") + 1 ))
else
    hard_count=0
fi
printf '%s\n' "$hard_count" > "$HARD_FAIL_FILE"

rule="none"
if [[ "$fail_kind" == "hard" ]] && (( hard_count >= FAIL_THRESHOLD )); then
    rule="hard_streak"
elif (( fail_count >= SOFT_FAIL_THRESHOLD )); then
    rule="soft_streak"
fi
decision_detail="kind=${fail_kind} fail=${fail_count}/${SOFT_FAIL_THRESHOLD} hard=${hard_count}/${FAIL_THRESHOLD} rule=${rule}"

active_detail="$(slot_detail "$active_port" "$active_container")"
audit "check" "${active_container}:${active_port}" "failed" "${decision_detail} ${ACTIVE_PROBE_DETAIL} recheck: ${active_detail}"
log "WARN active unhealthy ${decision_detail} ${ACTIVE_PROBE_DETAIL} ${active_container}:${active_port} recheck: ${active_detail}"

if [[ "$rule" == "none" ]]; then
    exit 0
fi

if [[ "$rule" == "soft_streak" ]]; then
    cooldown_remaining="$(failover_cooldown_remaining)"
    if (( cooldown_remaining > 0 )); then
        audit "failover" "${peer_container}:${peer_port}" "blocked" "cooldown remaining=${cooldown_remaining}s ${decision_detail} active_probe: ${ACTIVE_PROBE_DETAIL}"
        log "WARN failover blocked by cooldown remaining=${cooldown_remaining}s ${decision_detail}"
        exit 0
    fi
fi

if slot_healthy "$peer_port" "$peer_container"; then
    PEER_PROBE_DETAIL="$LAST_PROBE_DETAIL"
    switch_to_peer "$active_port" "$peer_port" "$active_container" "$peer_container"
    reset_fail_counters
    exit 0
fi

PEER_PROBE_DETAIL="$LAST_PROBE_DETAIL"
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
            reset_fail_counters
            audit "supervisor-start" "${active_container}:aads-api" "success" "health restored"
            log "RECOVERED stopped API child on ${active_container}:${active_port}"
            exit 0
        fi
    done
    audit "supervisor-start" "${active_container}:aads-api" "failed" "health not restored within 20 seconds"
fi

audit "recovery" "aads-api" "failed" "both slots unhealthy; no restart loop performed active_probe: ${ACTIVE_PROBE_DETAIL} peer_probe: ${PEER_PROBE_DETAIL}"
log "ERROR both API slots unhealthy; manual intervention required"
exit 1
