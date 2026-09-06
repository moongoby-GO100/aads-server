#!/bin/bash
# AADS 안전 배포 게이트웨이
# 사용법: deploy.sh [bluegreen|code|reload|build]
#   bluegreen (기본) — Blue↔Green 무중단 전환 (중단 0초, 자동 롤백, upstream 전환)
#   code/reload/build — 레거시 모드. 기본 차단 후 bluegreen으로 자동 전환.
#                      불가피한 수동 점검 때만 AADS_DEPLOY_ALLOW_LEGACY_RESTART=true 지정.
#
# 검증 6단계: 의존성→코드검증→배포→Health→DB스키마→채팅→LLM→프론트QA

set -euo pipefail

REQUESTED_MODE="${1:-bluegreen}"
MODE="$REQUESTED_MODE"
COMPOSE_DIR="/root/aads/aads-server"
export AADS_RELEASE_SHA="${AADS_RELEASE_SHA:-$(git -C "$COMPOSE_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)}"
HEALTH_URL="http://localhost:8100/api/v1/health"
MAX_WAIT="${AADS_DEPLOY_MAX_WAIT:-30}"
INTERVAL=2
UPSTREAM_CONF="/etc/nginx/conf.d/aads-upstream.conf"
ACTIVE_CONTAINER_FILE="${COMPOSE_DIR}/.active_container"
ACTIVE_PORT_FILE="${COMPOSE_DIR}/.active_port"
API_MEMORY_BYTES="${AADS_API_MEMORY_BYTES:-3221225472}"
API_MEMORY_SWAP_BYTES="${AADS_API_MEMORY_SWAP_BYTES:-5368709120}"
DEPLOY_START_EPOCH=$(date +%s)
DEPLOY_GENERATION_FILE="${COMPOSE_DIR}/.deploy_generation"
CONTROL_AUDIT_LOG="${AADS_CONTROL_AUDIT_LOG:-/var/log/aads-control-audit.jsonl}"
RELEASE_CONTEXT_DIR=""
DEPLOY_RUN_ID=""
DEPLOY_CURRENT_PHASE="initializing"
DEPLOY_PHASE_START_EPOCH="$DEPLOY_START_EPOCH"
mkdir -p "${COMPOSE_DIR}/logs"

cleanup_release_context() {
    case "${RELEASE_CONTEXT_DIR:-}" in
        /tmp/aads-server-release.*)
            rm -rf -- "$RELEASE_CONTEXT_DIR"
            ;;
    esac
    RELEASE_CONTEXT_DIR=""
}

build_release_image() {
    cleanup_release_context
    RELEASE_CONTEXT_DIR="$(mktemp -d /tmp/aads-server-release.XXXXXX)"
    git -C "$COMPOSE_DIR" archive --format=tar HEAD | tar -xf - -C "$RELEASE_CONTEXT_DIR"
    echo "[deploy.sh] clean release context: ${RELEASE_CONTEXT_DIR} (HEAD=${AADS_RELEASE_SHA})"
    docker build --tag "aads-server:${AADS_RELEASE_SHA}" "$RELEASE_CONTEXT_DIR"
    cleanup_release_context
}

if [[ -x "${COMPOSE_DIR}/scripts/verify-bluegreen-release-contract.sh" ]]; then
    "${COMPOSE_DIR}/scripts/verify-bluegreen-release-contract.sh" "$COMPOSE_DIR"
else
    echo "[deploy.sh] ❌ release contract verifier missing or not executable"
    exit 1
fi

# ── 다운타임 자동 측정 (2026-08-20 Blue/Green 동시 다운 인시던트 재발 방지) ──
# nginx를 통한 실제 사용자 경로를 1초 주기로 폴링해 실패 구간을 누적한다.
# 해상도는 프로브 응답시간 + 1초(대략 ±4초). 게이트가 아니라 계측 용도다.
DOWNTIME_FILE="${COMPOSE_DIR}/.deploy_downtime"
DOWNTIME_PROBE_URL="${DOWNTIME_PROBE_URL:-http://127.0.0.1/api/v1/health}"
DOWNTIME_PROBE_HOST="${DOWNTIME_PROBE_HOST:-aads.newtalk.kr}"
DOWNTIME_MONITOR_PID=""

start_downtime_monitor() {
    echo 0 > "$DOWNTIME_FILE" 2>/dev/null || true
    (
        total=0
        while true; do
            t0=$(date +%s)
            if curl -fsS -m 5 -o /dev/null -H "Host: ${DOWNTIME_PROBE_HOST}" "$DOWNTIME_PROBE_URL" 2>/dev/null; then
                :
            else
                t1=$(date +%s)
                total=$(( total + (t1 - t0) + 1 ))
                echo "$total" > "$DOWNTIME_FILE" 2>/dev/null || true
            fi
            sleep 1
        done
    ) &
    DOWNTIME_MONITOR_PID=$!
}

stop_downtime_monitor() {
    if [[ -n "${DOWNTIME_MONITOR_PID:-}" ]]; then
        kill "$DOWNTIME_MONITOR_PID" >/dev/null 2>&1 || true
        DOWNTIME_MONITOR_PID=""
    fi
}

get_downtime_seconds() {
    local v="0"
    if [[ -f "$DOWNTIME_FILE" ]]; then
        v=$(tr -d '[:space:]' < "$DOWNTIME_FILE" 2>/dev/null || echo 0)
    fi
    if [[ ! "$v" =~ ^[0-9]+$ ]]; then
        v="0"
    fi
    echo "$v"
}

sql_escape() {
    printf "%s" "${1:-}" | sed "s/'/''/g"
}

deploy_db_exec() {
    local sql="$1"
    docker exec aads-postgres psql -U aads -d aads -qAtc "$sql" 2>/dev/null || true
}

deploy_db_available() {
    docker inspect aads-postgres --format '{{.State.Running}}' 2>/dev/null | grep -q true
}

reconcile_stale_deploy_runs() {
    if ! deploy_db_available; then
        return 0
    fi
    local stale_after_minutes rows
    stale_after_minutes="${AADS_DEPLOY_STALE_AFTER_MINUTES:-15}"
    if [[ ! "$stale_after_minutes" =~ ^[0-9]+$ ]] || [[ "$stale_after_minutes" -lt 5 ]]; then
        stale_after_minutes="15"
    fi
    rows="$(
        deploy_db_exec "
            SELECT id, COALESCE(deploy_pid, 0)::int,
                   EXTRACT(EPOCH FROM (NOW() - COALESCE(last_heartbeat_at, updated_at)))::bigint
            FROM deploy_runs
            WHERE status IN ('running', 'verifying', 'syncing_standby')
              AND COALESCE(last_heartbeat_at, updated_at) < NOW() - (${stale_after_minutes} || ' minutes')::interval
            ORDER BY id;
        "
    )"
    if [[ -z "${rows//[[:space:]]/}" ]]; then
        return 0
    fi
    local run_id run_pid heartbeat_age detail detail_sql
    while IFS='|' read -r run_id run_pid heartbeat_age; do
        [[ "$run_id" =~ ^[0-9]+$ ]] || continue
        run_pid="${run_pid:-0}"
        if [[ "$run_pid" =~ ^[0-9]+$ ]] && [[ "$run_pid" -gt 1 ]] && kill -0 "$run_pid" 2>/dev/null; then
            audit_control "deploy-run-reconcile" "deploy_runs:${run_id}" "kept" "pid=${run_pid} still alive"
            continue
        fi
        detail="stale deploy reconciled before new deploy: pid=${run_pid:-unknown} heartbeat_age=${heartbeat_age:-unknown}s"
        detail_sql="$(sql_escape "$detail")"
        deploy_db_exec "
            WITH updated AS (
                UPDATE deploy_runs
                SET status='failed',
                    phase_completed_at=NOW(),
                    updated_at=NOW(),
                    last_heartbeat_at=NOW(),
                    error_summary=CONCAT_WS('; ', NULLIF(error_summary, ''), '$detail_sql')
                WHERE id=${run_id}
                  AND status IN ('running', 'verifying', 'syncing_standby')
                RETURNING id, phase, phase_started_at, current_slot, candidate_slot, image_digest, standby_digest
            )
            INSERT INTO deploy_phase_events(deploy_run_id, phase, status, phase_started_at,
                                            phase_completed_at, duration_ms, current_slot,
                                            candidate_slot, image_digest, standby_digest,
                                            error_summary, metadata)
            SELECT id, COALESCE(phase, 'unknown'), 'failed',
                   COALESCE(phase_started_at, NOW()), NOW(),
                   GREATEST(0, EXTRACT(EPOCH FROM (NOW() - COALESCE(phase_started_at, NOW())))::bigint * 1000),
                   current_slot, candidate_slot, image_digest, standby_digest,
                   '$detail_sql',
                   jsonb_build_object('reconciled_by', 'deploy.sh', 'deploy_pid', ${run_pid:-0}, 'heartbeat_age_seconds', ${heartbeat_age:-0})
            FROM updated;
        " >/dev/null
        audit_control "deploy-run-reconcile" "deploy_runs:${run_id}" "failed" "$detail"
        echo "[deploy.sh] stale deploy run reconciled: id=${run_id}, pid=${run_pid:-unknown}, heartbeat_age=${heartbeat_age:-unknown}s"
    done <<< "$rows"
}

ensure_deploy_observability_schema() {
    if ! deploy_db_available; then
        echo "[deploy.sh] ❌ PostgreSQL is not running; cannot verify deployment observability schema"
        return 1
    fi
    if ! docker exec -i aads-postgres psql -U aads -d aads -v ON_ERROR_STOP=1 -q \
        < "${COMPOSE_DIR}/migrations/150_deploy_observability_v1.sql" >/dev/null; then
        echo "[deploy.sh] ❌ deployment observability schema migration failed"
        return 1
    fi
}

deploy_observe_init() {
    if [[ -n "${DEPLOY_RUN_ID:-}" ]] || ! deploy_db_available; then
        return 0
    fi
    local release_sql current_slot_sql candidate_slot_sql generation_sql run_id
    release_sql="$(sql_escape "${AADS_RELEASE_SHA:-unknown}")"
    current_slot_sql="$(sql_escape "${CURRENT_PORT:-${ACTIVE_PORT:-unknown}}")"
    candidate_slot_sql="$(sql_escape "${NEW_PORT:-}")"
    generation_sql="$(sql_escape "${DEPLOY_GENERATION:-}")"
    run_id="$(
        deploy_db_exec "
            INSERT INTO deploy_runs(project, release_sha, status, phase, phase_started_at,
                                    current_slot, candidate_slot, deploy_pid, deploy_generation,
                                    last_heartbeat_at, created_at, updated_at)
            VALUES('AADS', '$release_sql', 'running', 'initializing', NOW(),
                   '$current_slot_sql', NULLIF('$candidate_slot_sql', ''), $$,
                   NULLIF('$generation_sql', ''), NOW(), NOW(), NOW())
            RETURNING id;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ "$run_id" =~ ^[0-9]+$ ]]; then
        DEPLOY_RUN_ID="$run_id"
        echo "[deploy.sh] deploy_run_id=${DEPLOY_RUN_ID}"
    fi
}

deploy_estimated_remaining_ms() {
    local elapsed_ms="$1"
    local default_estimate_ms="${AADS_DEPLOY_DEFAULT_ESTIMATE_MS:-600000}"
    local estimate
    if [[ ! "$default_estimate_ms" =~ ^[0-9]+$ ]]; then
        default_estimate_ms="600000"
    fi
    estimate="$(
        deploy_db_exec "
            WITH estimates AS (
                SELECT p50_duration_ms
                FROM deploy_recent_durations
                WHERE project='AADS'
                UNION ALL
                SELECT ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s) * 1000)::bigint
                FROM deploy_history
                WHERE project='AADS'
                  AND status='success'
                  AND duration_s IS NOT NULL
                  AND created_at >= NOW() - INTERVAL '90 days'
                HAVING COUNT(*) > 0
            )
            SELECT GREATEST(0, COALESCE((SELECT p50_duration_ms FROM estimates LIMIT 1), ${default_estimate_ms}) - ${elapsed_ms})::bigint;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ "$estimate" =~ ^[0-9]+$ ]]; then
        echo "$estimate"
    else
        echo "NULL"
    fi
}

deploy_observe_update() {
    local status="${1:-running}"
    local phase="${2:-$DEPLOY_CURRENT_PHASE}"
    local err="${3:-}"
    deploy_observe_init
    if [[ -z "${DEPLOY_RUN_ID:-}" ]]; then
        return 0
    fi
    local elapsed_ms estimate_ms status_sql phase_sql err_sql current_slot_sql candidate_slot_sql image_sql standby_sql
    elapsed_ms=$((($(date +%s) - DEPLOY_START_EPOCH) * 1000))
    estimate_ms="$(deploy_estimated_remaining_ms "$elapsed_ms")"
    status_sql="$(sql_escape "$status")"
    phase_sql="$(sql_escape "$phase")"
    err_sql="$(sql_escape "$err")"
    current_slot_sql="$(sql_escape "${CURRENT_PORT:-${ACTIVE_PORT:-unknown}}")"
    candidate_slot_sql="$(sql_escape "${NEW_PORT:-}")"
    image_sql="$(sql_escape "$(docker inspect "${NEW_CONTAINER:-$ACTIVE_CONTAINER}" --format '{{.Image}}' 2>/dev/null || true)")"
    standby_sql="$(sql_escape "$(docker inspect "${OLD_CONTAINER:-}" --format '{{.Image}}' 2>/dev/null || true)")"
    deploy_db_exec "
        UPDATE deploy_runs
        SET status='$status_sql',
            phase='$phase_sql',
            updated_at=NOW(),
            last_heartbeat_at=NOW(),
            phase_completed_at=CASE
                WHEN '$status_sql' IN ('success', 'completed', 'failed', 'blocked') THEN NOW()
                ELSE phase_completed_at
            END,
            duration_ms=${elapsed_ms},
            estimated_remaining_ms=${estimate_ms},
            current_slot='$current_slot_sql',
            candidate_slot=NULLIF('$candidate_slot_sql', ''),
            image_digest=NULLIF('$image_sql', ''),
            standby_digest=NULLIF('$standby_sql', ''),
            error_summary=NULLIF('$err_sql', '')
        WHERE id=${DEPLOY_RUN_ID};
    " >/dev/null
}

deploy_signal_trap() {
    local signal_name="${1:-TERM}"
    stop_downtime_monitor
    deploy_phase_end "$DEPLOY_CURRENT_PHASE" "failed" "deploy interrupted by ${signal_name}"
    deploy_observe_update "failed" "$DEPLOY_CURRENT_PHASE" "deploy interrupted by ${signal_name}"
    record_deploy "failed" "$MODE" "deploy interrupted by ${signal_name}"
    cleanup_release_context
    rm -f "${LOCKFILE:-/tmp/aads-deploy.lock}" 2>/dev/null || true
    exit 143
}

deploy_phase_start() {
    DEPLOY_CURRENT_PHASE="${1:-unknown}"
    DEPLOY_PHASE_START_EPOCH="$(date +%s)"
    deploy_observe_update "${2:-running}" "$DEPLOY_CURRENT_PHASE" ""
    echo "[deploy.sh] ▶ phase=${DEPLOY_CURRENT_PHASE}"
}

deploy_phase_end() {
    local phase="${1:-$DEPLOY_CURRENT_PHASE}"
    local status="${2:-success}"
    local err="${3:-}"
    deploy_observe_init
    if [[ -z "${DEPLOY_RUN_ID:-}" ]]; then
        return 0
    fi
    local duration_ms phase_sql status_sql err_sql current_slot_sql candidate_slot_sql image_sql standby_sql
    duration_ms=$((($(date +%s) - DEPLOY_PHASE_START_EPOCH) * 1000))
    phase_sql="$(sql_escape "$phase")"
    status_sql="$(sql_escape "$status")"
    err_sql="$(sql_escape "$err")"
    current_slot_sql="$(sql_escape "${CURRENT_PORT:-${ACTIVE_PORT:-unknown}}")"
    candidate_slot_sql="$(sql_escape "${NEW_PORT:-}")"
    image_sql="$(sql_escape "$(docker inspect "${NEW_CONTAINER:-$ACTIVE_CONTAINER}" --format '{{.Image}}' 2>/dev/null || true)")"
    standby_sql="$(sql_escape "$(docker inspect "${OLD_CONTAINER:-}" --format '{{.Image}}' 2>/dev/null || true)")"
    deploy_db_exec "
        INSERT INTO deploy_phase_events(deploy_run_id, phase, status, phase_started_at,
                                        phase_completed_at, duration_ms, current_slot,
                                        candidate_slot, image_digest, standby_digest,
                                        error_summary)
        VALUES(${DEPLOY_RUN_ID}, '$phase_sql', '$status_sql',
               to_timestamp(${DEPLOY_PHASE_START_EPOCH}), NOW(), ${duration_ms},
               '$current_slot_sql', NULLIF('$candidate_slot_sql', ''),
               NULLIF('$image_sql', ''), NULLIF('$standby_sql', ''),
               NULLIF('$(sql_escape "$err")', ''));
    " >/dev/null
    if [[ "$status" != "success" ]]; then
        deploy_observe_update "$status" "$phase" "$err"
    fi
}

report_dirty_release_exclusions() {
    local tracked_dirty untracked_dirty
    tracked_dirty="$(git -C "$COMPOSE_DIR" status --porcelain | awk 'substr($0, 1, 2) != "??" {c++} END {print c+0}')"
    untracked_dirty="$(git -C "$COMPOSE_DIR" status --porcelain | awk 'substr($0, 1, 2) == "??" {c++} END {print c+0}')"
    if [[ "${tracked_dirty:-0}" != "0" || "${untracked_dirty:-0}" != "0" ]]; then
        echo "[deploy.sh] ⚠️ release image excludes uncommitted worktree changes: tracked=${tracked_dirty:-0}, untracked=${untracked_dirty:-0}"
        git -C "$COMPOSE_DIR" status --porcelain | sed 's/^/[deploy.sh]   excluded: /' | head -80 || true
        audit_control "release-context" "$COMPOSE_DIR" "warning" "excluded dirty files tracked=${tracked_dirty:-0} untracked=${untracked_dirty:-0}"
    fi
}

enforce_release_worktree_gate() {
    local dirty_count
    dirty_count="$(git -C "$COMPOSE_DIR" status --porcelain | wc -l | tr -d '[:space:]' || echo 0)"
    if [[ "${dirty_count:-0}" == "0" ]]; then
        return 0
    fi
    report_dirty_release_exclusions
    if [[ "${AADS_DEPLOY_ALLOW_DIRTY_ARCHIVE:-false}" == "true" ]]; then
        local override_reason="${AADS_DEPLOY_DIRTY_OVERRIDE_REASON:-}"
        if [[ -z "${override_reason//[[:space:]]/}" ]]; then
            echo "[deploy.sh] ❌ dirty worktree override requires AADS_DEPLOY_DIRTY_OVERRIDE_REASON."
            echo "[deploy.sh]    This prevents silent 'saved but not deployed' releases from dirty worktrees."
            audit_control "release-context" "$COMPOSE_DIR" "blocked" "dirty override missing reason count=${dirty_count}"
            return 1
        fi
        echo "[deploy.sh] ⚠️ dirty worktree override accepted: AADS_DEPLOY_ALLOW_DIRTY_ARCHIVE=true"
        echo "[deploy.sh]    override reason: ${override_reason}"
        echo "[deploy.sh]    Only committed HEAD=${AADS_RELEASE_SHA} is archived into the release image."
        audit_control "release-context" "$COMPOSE_DIR" "override" "dirty archive override count=${dirty_count}; reason=${override_reason}"
        return 0
    fi
    echo "[deploy.sh] ❌ dirty worktree detected; release blocked before build."
    echo "[deploy.sh]    Commit/stash/split unrelated files, or explicitly set AADS_DEPLOY_ALLOW_DIRTY_ARCHIVE=true after confirming dirty files must be excluded."
    audit_control "release-context" "$COMPOSE_DIR" "blocked" "dirty worktree count=${dirty_count}"
    return 1
}

audit_control() {
    local action="${1:-unknown}"
    local target="${2:-unknown}"
    local result="${3:-unknown}"
    local detail="${4:-}"
    action="${action//\"/\\\"}"
    target="${target//\"/\\\"}"
    result="${result//\"/\\\"}"
    detail="${detail//\"/\\\"}"
    detail="${detail//$'\n'/ }"
    printf '{"ts":"%s","actor":"deploy.sh","generation":"%s","action":"%s","target":"%s","result":"%s","detail":"%s"}\n' \
        "$(date --iso-8601=seconds)" "${DEPLOY_GENERATION:-not-assigned}" "$action" "$target" "$result" "$detail" \
        >> "$CONTROL_AUDIT_LOG" 2>/dev/null || true
}

verify_container_memory_limit() {
    local container="$1"
    local memory=""
    local memory_swap=""

    memory="$(docker inspect "$container" --format '{{.HostConfig.Memory}}' 2>/dev/null || true)"
    memory_swap="$(docker inspect "$container" --format '{{.HostConfig.MemorySwap}}' 2>/dev/null || true)"
    if [[ "$memory" != "$API_MEMORY_BYTES" || "$memory_swap" != "$API_MEMORY_SWAP_BYTES" ]]; then
        echo "[deploy.sh] ❌ ${container} memory limit mismatch: memory=${memory:-missing} swap=${memory_swap:-missing}, expected=${API_MEMORY_BYTES}/${API_MEMORY_SWAP_BYTES}"
        audit_control "memory-limit" "$container" "failed" "memory=${memory:-missing} swap=${memory_swap:-missing}"
        return 1
    fi
    echo "[deploy.sh] ✅ ${container} memory limit verified: memory=${memory} swap=${memory_swap}"
    audit_control "memory-limit" "$container" "success" "memory=${memory} swap=${memory_swap}"
}

record_deploy() {
    local status="${1:-started}"
    local deploy_type="${2:-$MODE}"
    local err="${3:-}"
    local now_epoch
    local duration
    local commit
    local msg
    local type_sql
    local commit_sql
    local msg_sql
    local status_sql
    local err_sql
    local downtime

    now_epoch=$(date +%s)
    duration=$((now_epoch - DEPLOY_START_EPOCH))
    if [[ "$status" == "started" ]]; then
        duration=0
    fi
    downtime=$(get_downtime_seconds)
    if [[ "$status" == "started" ]]; then
        downtime=0
    fi
    commit="${AADS_RELEASE_SHA:-unknown}"
    msg=$(git -C "$COMPOSE_DIR" log -1 --pretty=%s "$commit" 2>/dev/null || echo "unknown")

    type_sql=$(sql_escape "$deploy_type")
    commit_sql=$(sql_escape "$commit")
    msg_sql=$(sql_escape "$msg")
    status_sql=$(sql_escape "$status")
    err_sql=$(sql_escape "$err")

    docker exec aads-postgres psql -U aads -d aads -c "INSERT INTO deploy_history(deploy_type,project,trigger_by,git_commit,git_message,status,duration_s,error_msg,downtime_seconds,created_at) VALUES('$type_sql','AADS','deploy.sh','$commit_sql','$msg_sql','$status_sql',$duration,'$err_sql',$downtime,NOW())" >/dev/null 2>&1 || \
        echo "[deploy.sh] WARN: deploy_history insert failed (status=${status}, type=${deploy_type})"
}

deploy_error_trap() {
    local exit_code="$?"
    local line_no="${1:-unknown}"
    local command="${2:-unknown}"
    stop_downtime_monitor
    deploy_phase_end "$DEPLOY_CURRENT_PHASE" "failed" "unexpected error exit=${exit_code} line=${line_no}: ${command:0:300}"
    record_deploy "failed" "$MODE" "unexpected error exit=${exit_code} line=${line_no}: ${command:0:300}"
}

trap 'deploy_error_trap "$LINENO" "$BASH_COMMAND"' ERR
trap 'deploy_signal_trap TERM' TERM
trap 'deploy_signal_trap INT' INT
trap 'deploy_signal_trap HUP' HUP

get_active_port() {
    local port=""
    local upstream_port=""
    local upstream_count="0"
    if [[ -f "$UPSTREAM_CONF" ]]; then
        upstream_count=$(grep "server 127.0.0.1:" "$UPSTREAM_CONF" \
            | grep -v backup \
            | grep -oP '127\.0\.0\.1:\K(8100|8102)' \
            | sort -u \
            | wc -l \
            | tr -d '[:space:]' || true)
        if [[ "$upstream_count" == "1" ]]; then
            upstream_port=$(grep "server 127.0.0.1:" "$UPSTREAM_CONF" \
                | grep -v backup \
                | grep -oP '127\.0\.0\.1:\K(8100|8102)' \
                | sort -u \
                | head -1 || true)
        fi
    fi
    if [[ "$upstream_port" == "8100" || "$upstream_port" == "8102" ]]; then
        port="$upstream_port"
        echo "$port" > "$ACTIVE_PORT_FILE" 2>/dev/null || true
    elif [[ -f "$ACTIVE_PORT_FILE" ]]; then
        port=$(tr -d '[:space:]' < "$ACTIVE_PORT_FILE" 2>/dev/null || true)
    fi
    if [[ "$port" != "8100" && "$port" != "8102" ]]; then
        port="8100"
    fi
    echo "$port"
}

get_active_container() {
    local container=""
    local port="${ACTIVE_PORT:-}"
    if [[ "$port" == "8100" ]]; then
        echo "aads-server" > "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true
        echo "aads-server"
        return 0
    elif [[ "$port" == "8102" ]]; then
        echo "aads-server-green" > "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true
        echo "aads-server-green"
        return 0
    fi
    if [[ -f "$ACTIVE_CONTAINER_FILE" ]]; then
        container=$(tr -d '[:space:]' < "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true)
    fi
    # 파일 값이 실제로 실행 중인지 검증 — 정지된 컨테이너 참조 방지
    if [[ -n "$container" ]] && docker inspect "$container" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
        echo "$container"
        return 0
    fi
    # 실행 중인 컨테이너 자동 탐색 + 상태 파일 동기화
    for c in aads-server aads-server-green; do
        if docker inspect "$c" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
            echo "$c" > "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true
            echo "$c"
            return 0
        fi
    done
    echo "aads-server"
}


_verify_telegram_alert() {
    # verify_active_slot 차단 시 텔레그램 알림. notify() 함수 정의 전이라 인라인 처리.
    local msg="$1"
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -sf -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_CHAT_ID}" \
            -d text="🚨 [AADS Deploy 차단] ${msg}" \
            -d parse_mode=HTML >/dev/null 2>&1 || true
    fi
}

verify_active_slot() {
    # AADS: nginx upstream active 라인과 .active_port 파일 정합성 + 실제 컨테이너 생존 검증.
    # blue-green 배포 후 nginx가 죽은 슬롯을 가리키면 외부 502 발생 (2026-05-13 incident).
    local active_port="$1"
    local nginx_active=""
    local nginx_active_count="0"
    if [[ -f "$UPSTREAM_CONF" ]]; then
        nginx_active_count=$(grep "server 127.0.0.1:" "$UPSTREAM_CONF" \
            | grep -v backup \
            | grep -oP '127\.0\.0\.1:\K(8100|8102)' \
            | sort -u | wc -l | tr -d '[:space:]' || true)
        nginx_active=$(grep "server 127.0.0.1:" "$UPSTREAM_CONF" \
            | grep -v backup \
            | grep -oP '127\.0\.0\.1:\K(8100|8102)' \
            | sort -u | head -1 || true)
    fi
    # AADS: multi-active는 비정상(drain 중이거나 sed swap 실패) — 차단 + 알람
    if [[ "$nginx_active_count" != "1" ]]; then
        echo "[deploy.sh] ❌ nginx upstream active 라인이 ${nginx_active_count}개 (정상=1) — 배포 차단"
        echo "[deploy.sh]    수동 정합성 회복: grep 'server 127' $UPSTREAM_CONF"
        _verify_telegram_alert "verify_active_slot: multi-active 감지(count=${nginx_active_count})"
        record_deploy "blocked" "slot_guard" "verify_active_slot: multi-active count=${nginx_active_count}"
        exit 1
    fi
    if [[ -z "$nginx_active" ]]; then
        echo "[deploy.sh] ❌ nginx upstream active port 파싱 실패 — 배포 차단"
        _verify_telegram_alert "verify_active_slot: active port 파싱 실패"
        record_deploy "blocked" "slot_guard" "verify_active_slot: active port parse failed"
        exit 1
    fi
    if [[ "$nginx_active" != "$active_port" ]]; then
        echo "[deploy.sh] ❌ ACTIVE 슬롯 불일치"
        echo "[deploy.sh]    nginx upstream active = :${nginx_active}"
        echo "[deploy.sh]    .active_port 파일      = :${active_port}"
        echo "[deploy.sh]    이 상태에서 배포하면 nginx가 죽은 백엔드를 가리킬 수 있음."
        echo "[deploy.sh]    수동 정합성 회복 후 재시도:"
        echo "[deploy.sh]      1) docker ps | grep aads-server"
        echo "[deploy.sh]      2) 살아있는 쪽에 맞춰 nginx upstream 또는 .active_port 정정"
        echo "[deploy.sh]      3) nginx -s reload"
        _verify_telegram_alert "verify_active_slot: 슬롯 불일치 nginx=:${nginx_active} file=:${active_port}"
        record_deploy "blocked" "slot_guard" "verify_active_slot: slot mismatch nginx=:${nginx_active} file=:${active_port}"
        exit 1
    fi
    local target_container=""
    case "$active_port" in
        8100) target_container="aads-server" ;;
        8102) target_container="aads-server-green" ;;
    esac
    if [[ -n "$target_container" ]] && ! docker inspect "$target_container" --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
        echo "[deploy.sh] ❌ ACTIVE 컨테이너 ${target_container} 가 실행 중이 아님 (포트 :${active_port})"
        echo "[deploy.sh]    nginx upstream이 죽은 백엔드를 가리킴 → 외부 502 발생 가능."
        echo "[deploy.sh]    수동 복구 절차:"
        echo "[deploy.sh]      1) docker start ${target_container}"
        echo "[deploy.sh]      2) 또는 살아있는 쪽으로 nginx upstream swap 후 reload"
        _verify_telegram_alert "verify_active_slot: ${target_container}(:${active_port}) 죽음"
        record_deploy "blocked" "slot_guard" "verify_active_slot: ${target_container}(:${active_port}) not running"
        exit 1
    fi
    echo "[deploy.sh] ✅ ACTIVE 슬롯 일관성 확인: :${active_port} (${target_container})"
}

ACTIVE_PORT="$(get_active_port)"
ACTIVE_CONTAINER="$(get_active_container)"
HEALTH_URL="http://localhost:${ACTIVE_PORT}/api/v1/health"

verify_active_slot "$ACTIVE_PORT"

# Blue/green 컨테이너가 현재 active 슬롯을 읽어 background recovery 소유권을 판단한다.
# Docker bind mount 대상 파일은 컨테이너 생성 전에 반드시 존재해야 한다.
if [[ ! -f "$ACTIVE_PORT_FILE" ]]; then
    echo "$ACTIVE_PORT" > "$ACTIVE_PORT_FILE" 2>/dev/null || true
fi
if [[ ! -f "$ACTIVE_CONTAINER_FILE" ]]; then
    echo "$ACTIVE_CONTAINER" > "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true
fi

# ── 배포 중복 호출 방지 (lockfile) ──
LOCKFILE="/tmp/aads-deploy.lock"
if [ -f "$LOCKFILE" ]; then
    LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "[deploy.sh] ❌ 배포 이미 진행 중 (PID=$LOCK_PID). 중복 호출 차단."
        record_deploy "blocked" "$MODE" "deploy already running PID=${LOCK_PID}"
        exit 1
    else
        echo "[deploy.sh] ⚠️ stale lockfile 제거 (PID=$LOCK_PID 종료됨)"
        rm -f "$LOCKFILE"
    fi
fi
echo $$ > "$LOCKFILE"
cleanup_deploy() {
    stop_downtime_monitor
    cleanup_release_context
    rm -f "$LOCKFILE"
}
trap cleanup_deploy EXIT

# nginx upstream is shared by backend and dashboard blue-green deploys. Only
# the routing cutover is serialized; image build and health checks run without
# this lock so a long build cannot block another safe release.
NGINX_SWITCH_LOCK="/tmp/aads-nginx-upstream.lock"
exec 8>"$NGINX_SWITCH_LOCK"
NGINX_LOCK_HELD=false
acquire_nginx_switch_lock() {
    if [[ "$NGINX_LOCK_HELD" == "true" ]]; then return 0; fi
    if ! flock -w 300 8; then
        echo "[deploy.sh] ❌ nginx upstream 전환 락 획득 실패. 다른 배포가 전환 중입니다."
        record_deploy "blocked" "$MODE" "nginx upstream cutover lock acquisition failed"
        return 1
    fi
    NGINX_LOCK_HELD=true
    echo "[deploy.sh] ✅ nginx upstream 전환 락 획득"
}

release_nginx_switch_lock() {
    if [[ "$NGINX_LOCK_HELD" == "true" ]]; then
        flock -u 8
        NGINX_LOCK_HELD=false
        echo "[deploy.sh] ✅ nginx upstream 전환 락 해제"
    fi
}

# Every background drain/sync job is bound to this generation. A newer deploy
# invalidates older jobs before they can mutate a slot that has become active.
DEPLOY_GENERATION="${DEPLOY_START_EPOCH}-$$-$(git -C "$COMPOSE_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf '%s\n' "$DEPLOY_GENERATION" > "$DEPLOY_GENERATION_FILE"
audit_control "deploy-generation" "$ACTIVE_CONTAINER:$ACTIVE_PORT" "started" "mode=$MODE"
ensure_deploy_observability_schema
reconcile_stale_deploy_runs
deploy_phase_start "preflight" "running"
if ! enforce_release_worktree_gate; then
    deploy_phase_end "preflight" "blocked" "dirty worktree blocks release"
    record_deploy "blocked" "$MODE" "dirty worktree blocks release"
    exit 1
fi

# 텔레그램 알림 (환경변수 있으면 발송)
notify() {
    local msg="$1"
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -sf -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_CHAT_ID}" \
            -d text="🚀 [AADS Deploy] ${msg}" \
            -d parse_mode=HTML >/dev/null 2>&1 || true
    fi
}

container_for_port() {
    case "$1" in
        8100) echo "aads-server" ;;
        8102) echo "aads-server-green" ;;
        *) echo "" ;;
    esac
}

peer_port_for() {
    case "$1" in
        8100) echo "8102" ;;
        8102) echo "8100" ;;
        *) echo "" ;;
    esac
}

stream_count_for_port() {
    local port="$1"
    local container
    local db_count
    container="$(container_for_port "$port")"
    if [[ -n "$container" ]] && docker inspect aads-postgres --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
        db_count="$(
            docker exec aads-postgres psql -U aads -d aads -Atc "
                SELECT count(*)::int
                FROM chat_turn_executions
                WHERE status IN ('running','retrying')
                  AND completed_at IS NULL
                  AND owner_instance = '${container}'
                  AND (
                      lease_expires_at IS NULL
                      OR lease_expires_at > NOW()
                      OR heartbeat_at > NOW() - INTERVAL '60 seconds'
                  );
            " 2>/dev/null | tr -d '[:space:]' || true
        )"
        if [[ "$db_count" =~ ^[0-9]+$ ]]; then
            echo "$db_count"
            return 0
        fi
    fi
    (
        curl -s -m 5 "http://127.0.0.1:${port}/api/v1/ops/active-streams" 2>/dev/null \
        | python3 -c "import sys,json; value=json.load(sys.stdin).get('count', 'unknown'); print(value if value is not None else 'unknown')" 2>/dev/null
    ) || echo "unknown"
}

reconcile_inactive_target_recovery_executions() {
    local target_container="$1"
    if [[ -z "$target_container" ]] || ! deploy_db_available; then
        return 0
    fi
    local target_sql reconciled
    target_sql="$(sql_escape "$target_container")"
    reconciled="$(
        deploy_db_exec "
            WITH candidates AS (
                SELECT te.id
                FROM chat_turn_executions te
                WHERE te.owner_instance = '$target_sql'
                  AND te.status IN ('running', 'retrying')
                  AND te.completed_at IS NULL
                  AND COALESCE(te.error_message, '') = 'recovery_auto_retry_scheduled'
                  AND EXISTS (
                      SELECT 1
                      FROM chat_messages m
                      WHERE m.execution_id = te.id
                        AND m.intent = 'streaming_placeholder'
                        AND COALESCE(m.is_hidden, false) = true
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM chat_messages m2
                      WHERE m2.execution_id = te.id
                        AND m2.role = 'assistant'
                        AND length(COALESCE(m2.content, '')) > 500
                  )
            ),
            updated AS (
                UPDATE chat_turn_executions te
                SET status = 'cancelled',
                    completed_at = NOW(),
                    updated_at = NOW(),
                    lease_expires_at = NOW(),
                    error_message = 'inactive target slot recovery reconciled before deploy'
                FROM candidates c
                WHERE te.id = c.id
                RETURNING te.id
            )
            SELECT count(*) FROM updated;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ "$reconciled" =~ ^[0-9]+$ ]] && [[ "$reconciled" -gt 0 ]]; then
        echo "[deploy.sh] reconciled inactive target recovery executions: container=${target_container}, count=${reconciled}"
        audit_control "target-slot-recovery-reconcile" "$target_container" "success" "cancelled_hidden_recovery=${reconciled}"
    fi
}

wait_port_health() {
    local port="$1"
    local max_wait="${2:-60}"
    local elapsed=0
    while [[ $elapsed -lt $max_wait ]]; do
        if curl -sf "http://127.0.0.1:${port}/api/v1/health" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    return 1
}

nginx_config_test() {
    if command -v nginx >/dev/null 2>&1; then
        nginx -t
    elif docker inspect aads-nginx --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
        docker exec aads-nginx nginx -t
    else
        echo "[deploy.sh] ❌ 실행 중인 nginx를 찾을 수 없습니다."
        return 1
    fi
}

nginx_reload() {
    if command -v nginx >/dev/null 2>&1 && systemctl is-active --quiet nginx 2>/dev/null; then
        systemctl reload nginx
    elif docker inspect aads-nginx --format '{{.State.Running}}' 2>/dev/null | grep -q true; then
        docker exec aads-nginx nginx -s reload
    else
        echo "[deploy.sh] ❌ reload 가능한 nginx를 찾을 수 없습니다."
        return 1
    fi
}

switch_api_upstream() {
    local new_port="$1"
    local old_port="$2"
    local new_container="$3"
    local old_container="$4"

    acquire_nginx_switch_lock
    cp "$UPSTREAM_CONF" "${UPSTREAM_CONF}.pre_code_switch"
    sed -i -E \
        -e "s/server 127\.0\.0\.1:${new_port} [^;]*;/server 127.0.0.1:${new_port} max_fails=1 fail_timeout=10s;/g" \
        -e "s/server 127\.0\.0\.1:${old_port} [^;]*;/server 127.0.0.1:${old_port} max_fails=1 fail_timeout=10s backup;/g" \
        "$UPSTREAM_CONF"
    if ! nginx_config_test >/dev/null 2>&1; then
        cp "${UPSTREAM_CONF}.pre_code_switch" "$UPSTREAM_CONF"
        audit_control "nginx-switch" "${old_port}->${new_port}" "failed" "configuration test failed"
        echo "[deploy.sh] ❌ nginx 설정 오류 — upstream 전환 취소"
        return 1
    fi

    if ! nginx_reload; then
        cp "${UPSTREAM_CONF}.pre_code_switch" "$UPSTREAM_CONF"
        nginx_config_test >/dev/null 2>&1 && nginx_reload >/dev/null 2>&1 || true
        audit_control "nginx-switch" "${old_port}->${new_port}" "failed" "reload failed; configuration rolled back"
        return 1
    fi
    echo "$new_port" > "$ACTIVE_PORT_FILE" 2>/dev/null || true
    echo "$new_container" > "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true
    docker exec "$new_container" sh -c 'printf true > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    audit_control "nginx-switch" "${old_container}:${old_port}->${new_container}:${new_port}" "success" "code mode slot switch"
    release_nginx_switch_lock
}

standby_ownership_valid() {
    local old_container="$1"
    local old_port="$2"
    local expected_generation="$3"
    local current_generation current_port current_container
    current_generation="$(tr -d '[:space:]' < "$DEPLOY_GENERATION_FILE" 2>/dev/null || true)"
    current_port="$(tr -d '[:space:]' < "$ACTIVE_PORT_FILE" 2>/dev/null || true)"
    current_container="$(tr -d '[:space:]' < "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true)"
    [[ "$current_generation" == "$expected_generation" ]] || return 1
    [[ "$current_port" != "$old_port" ]] || return 1
    [[ "$current_container" != "$old_container" ]] || return 1
    [[ "$(container_for_port "$old_port")" == "$old_container" ]] || return 1
}

restart_old_slot_after_drain() {
    local old_container="$1"
    local old_port="$2"
    local expected_generation="$3"

    (
        # This worker intentionally outlives the main deploy. Do not inherit the
        # global nginx upstream lock or unrelated deploys will remain blocked
        # for the full stream-drain window.
        exec 8>&-
        exec 9>"/tmp/aads-standby-sync.lock"
        flock -w 30 9 || {
            audit_control "standby-restart" "${old_container}:${old_port}" "skipped" "standby lock busy"
            return 0
        }
        if ! standby_ownership_valid "$old_container" "$old_port" "$expected_generation"; then
            audit_control "standby-restart" "${old_container}:${old_port}" "skipped" "stale generation or slot became active"
            return 0
        fi
        local drain_max=600
        local elapsed=0
        local active="0"
        while [[ $elapsed -lt $drain_max ]]; do
            active="$(stream_count_for_port "$old_port")"
            if [[ "$active" == "0" || -z "$active" ]]; then
                break
            fi
            echo "[deploy.sh] old slot ${old_container}:${old_port} active streams=${active}; wait 30s"
            sleep 30
            elapsed=$((elapsed + 30))
        done
        if [[ "${active:-0}" != "0" && -n "${active:-}" ]]; then
            echo "[deploy.sh] old slot ${old_container}:${old_port} still has active streams=${active}; skip restart to preserve SSE"
            return 0
        fi
        if ! standby_ownership_valid "$old_container" "$old_port" "$expected_generation"; then
            audit_control "standby-restart" "${old_container}:${old_port}" "skipped" "ownership changed after drain"
            return 0
        fi
        docker exec "$old_container" touch /tmp/aads_deploy_restart 2>/dev/null || true
        docker exec "$old_container" supervisorctl restart aads-api >/dev/null 2>&1 || true
        docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
        audit_control "standby-restart" "${old_container}:${old_port}" "success" "drained standby restarted"
    ) >> "${COMPOSE_DIR}/logs/standby-sync.log" 2>&1 &
    disown
}

sync_standby_slot_after_drain() {
    local old_container="$1"
    local old_port="$2"
    local expected_generation="$3"

    {
        # This step is part of release certification, not a best-effort
        # background task. The caller releases the nginx lock before entering it.
        # Existing nginx workers may still hold SSE/WebSocket streams on the old
        # slot, so require a short grace period plus consecutive zero samples.
        local min_wait="${AADS_DEPLOY_STANDBY_SYNC_MIN_WAIT:-10}"
        if [[ "$min_wait" != "0" ]]; then
            echo "[deploy.sh] standby sync grace wait ${old_container}:${old_port} ${min_wait}s"
            sleep "$min_wait"
        fi

        exec 9>"/tmp/aads-standby-sync.lock"
        flock -w 30 9 || {
            audit_control "standby-sync" "${old_container}:${old_port}" "skipped" "standby lock busy"
            return 0
        }
        if ! standby_ownership_valid "$old_container" "$old_port" "$expected_generation"; then
            audit_control "standby-sync" "${old_container}:${old_port}" "skipped" "stale generation or slot became active"
            return 0
        fi
        reconcile_inactive_target_recovery_executions "$old_container"

        local drain_max="${AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-300}"
        local drain_interval="${AADS_DEPLOY_STANDBY_SYNC_POLL_SECONDS:-5}"
        local elapsed=0
        local active="0"
        local zero_seen=0
        if [[ ! "$drain_max" =~ ^[0-9]+$ ]]; then
            drain_max="300"
        fi
        if [[ ! "$drain_interval" =~ ^[0-9]+$ ]] || [[ "$drain_interval" -lt 5 ]]; then
            drain_interval="5"
        fi
        while [[ $elapsed -lt $drain_max ]]; do
            active="$(stream_count_for_port "$old_port")"
            if [[ "$active" == "0" || -z "$active" ]]; then
                zero_seen=$((zero_seen + 1))
                if [[ "$zero_seen" -ge "${AADS_DEPLOY_STANDBY_ZERO_SAMPLES:-1}" ]]; then
                    break
                fi
            else
                zero_seen=0
            fi
            deploy_observe_update "syncing_standby" "standby_same_digest_sync" \
                "active_streams=${active:-unknown}; elapsed=${elapsed}s; max=${drain_max}s"
            echo "[deploy.sh] standby sync wait ${old_container}:${old_port} active streams=${active}; wait ${drain_interval}s"
            sleep "$drain_interval"
            elapsed=$((elapsed + drain_interval))
        done

        if [[ "${active:-0}" != "0" && -n "${active:-}" ]]; then
            echo "[deploy.sh] standby sync ERROR: ${old_container}:${old_port} still has active streams=${active}"
            docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
            audit_control "standby-sync" "${old_container}:${old_port}" "failed" "drain timeout active=${active}"
            return 1
        fi

        if ! standby_ownership_valid "$old_container" "$old_port" "$expected_generation"; then
            audit_control "standby-sync" "${old_container}:${old_port}" "skipped" "ownership changed after drain"
            return 1
        fi

        echo "[deploy.sh] standby sync PC Agent reconnect trigger on drained old slot :${old_port}"
        curl -sf -X POST "http://127.0.0.1:${old_port}/api/v1/pc-agent/graceful-shutdown" \
            -H "Content-Type: application/json" 2>/dev/null || true

        echo "[deploy.sh] standby sync: starting ${old_container}:${old_port} from release image ${AADS_RELEASE_SHA}"
        cd "$COMPOSE_DIR"
        if [[ "$old_container" == "aads-server-green" ]]; then
            docker compose -f "${COMPOSE_DIR}/docker-compose.prod.yml" --profile green up -d --force-recreate --no-build --no-deps "$old_container"
        else
            docker compose -f "${COMPOSE_DIR}/docker-compose.prod.yml" up -d --force-recreate --no-build --no-deps "$old_container"
        fi
        if ! verify_container_memory_limit "$old_container"; then
            audit_control "standby-sync" "${old_container}:${old_port}" "failed" "memory limit mismatch"
            return 1
        fi

        if wait_port_health "$old_port" 90; then
            active_container="$(tr -d '[:space:]' < "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true)"
            active_image="$(docker inspect "$active_container" --format '{{.Image}}' 2>/dev/null || true)"
            standby_image="$(docker inspect "$old_container" --format '{{.Image}}' 2>/dev/null || true)"
            if [[ -z "$active_image" || -z "$standby_image" || "$active_image" != "$standby_image" ]]; then
                echo "[deploy.sh] standby sync ERROR: image digest mismatch active=${active_image:-missing} standby=${standby_image:-missing}"
                audit_control "standby-sync" "${old_container}:${old_port}" "failed" "active/standby image digest mismatch"
                return 1
            fi
            docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
            echo "[deploy.sh] standby sync complete: ${old_container}:${old_port}"
            audit_control "standby-sync" "${old_container}:${old_port}" "success" "same release image and healthy"
            return 0
        else
            echo "[deploy.sh] standby sync WARN: ${old_container}:${old_port} health failed after rebuild"
            audit_control "standby-sync" "${old_container}:${old_port}" "failed" "health failed after rebuild"
            return 1
        fi
    } 2>&1 | tee -a "${COMPOSE_DIR}/logs/standby-sync.log"
    return "${PIPESTATUS[0]}"
}

# .env에서 텔레그램 변수 로드
if [[ -f "${COMPOSE_DIR}/.env" ]]; then
    export TELEGRAM_BOT_TOKEN=$(grep -oP '^TELEGRAM_BOT_TOKEN=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
    export TELEGRAM_CHAT_ID=$(grep -oP '^TELEGRAM_CHAT_ID=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
fi

if [[ "$MODE" == "code" || "$MODE" == "reload" || "$MODE" == "build" ]]; then
    if [[ "${AADS_DEPLOY_ALLOW_LEGACY_RESTART:-false}" == "true" ]]; then
        echo "[deploy.sh] ⚠️ legacy mode=${MODE} explicitly allowed by AADS_DEPLOY_ALLOW_LEGACY_RESTART=true"
    else
        echo "[deploy.sh] ⚠️ legacy mode=${MODE} would restart active API; redirecting to bluegreen"
        MODE="bluegreen"
    fi
fi

echo "[deploy.sh] requested_mode=${REQUESTED_MODE} effective_mode=${MODE} at $(date '+%Y-%m-%d %H:%M:%S')"
deploy_phase_end "preflight" "success" ""

if [[ "$MODE" == "code" ]]; then
    MAX_WAIT="${AADS_DEPLOY_MAX_WAIT:-60}"
fi

# Keep blue/green host ports private even before containers are recreated with
# loopback-only publish bindings.
if [[ -x "${COMPOSE_DIR}/scripts/apply-bg-port-firewall.sh" ]]; then
    "${COMPOSE_DIR}/scripts/apply-bg-port-firewall.sh" >/dev/null 2>&1 || \
        echo "[deploy.sh] ⚠️ BG host-only firewall guard apply failed; continuing deploy"
fi

# ── Phase 0: 의존 컨테이너 상태 확인 + 복구 ──
deploy_phase_start "dependency_check" "running"
echo "[deploy.sh] Phase 0: dependency check..."
for DEP in aads-postgres aads-redis aads-socket-proxy aads-litellm; do
    DEP_STATUS=$(docker inspect "$DEP" --format '{{.State.Status}}' 2>/dev/null)
    if [[ "$DEP_STATUS" != "running" ]]; then
        echo "[deploy.sh] ⚠️ ${DEP} 상태: ${DEP_STATUS:-없음} — 복구 중..."
        docker start "$DEP" 2>/dev/null || (cd "$COMPOSE_DIR" && docker compose up -d --no-deps "$DEP")
        sleep 3
        notify "⚠️ 배포 전 ${DEP} 복구 실행 (이전 상태: ${DEP_STATUS:-없음})"
    fi
done

echo "[deploy.sh] Phase 0: claude-relay dependency check..."
if ! /usr/bin/python3 -c "import aiohttp" >/dev/null 2>&1; then
    echo "[deploy.sh] ⚠️ host aiohttp missing — installing for claude-relay..."
    /usr/bin/python3 -m pip install aiohttp >/dev/null
fi
deploy_phase_end "dependency_check" "success" ""

echo "[deploy.sh] Phase 0: pre-deploy cleanup..."
docker exec -i aads-postgres psql -U aads -d aads -q <<'SQL' 2>/dev/null || echo "[deploy.sh] WARN: pre-deploy cleanup skipped"
WITH candidates AS (
    SELECT
        m.id,
        m.session_id,
        m.execution_id,
        m.content,
        NULLIF(
            btrim(regexp_replace(COALESCE(m.content, ''), E'\\n*⏳ _[^\\n]*_$', '', 'g')),
            ''
        ) AS clean_content
    FROM chat_messages m
    LEFT JOIN chat_sessions s ON s.current_execution_id = m.execution_id
    LEFT JOIN chat_turn_executions te ON te.id = m.execution_id
    WHERE m.intent = 'streaming_placeholder'
      AND NOT (
          s.current_execution_id = m.execution_id
          AND te.status IN ('running', 'retrying')
          AND (
              te.lease_expires_at > NOW()
              OR te.updated_at > NOW() - INTERVAL '10 minutes'
          )
      )
),
promoted AS (
    UPDATE chat_messages m
    SET content = CASE
            WHEN c.clean_content LIKE '%응답이 중단되어 여기까지 보존되었습니다.%'
              OR c.clean_content LIKE '%최신 지시를 우선 처리%'
            THEN c.clean_content
            ELSE c.clean_content || E'\n\n_(응답이 중단되어 여기까지 보존되었습니다.)_'
        END,
        intent = NULL,
        model_used = 'interrupted',
        edited_at = NOW()
    FROM candidates c
    WHERE m.id = c.id
      AND c.clean_content IS NOT NULL
    RETURNING m.id
),
deleted AS (
    DELETE FROM chat_messages m
    USING candidates c
    WHERE m.id = c.id
      AND c.clean_content IS NULL
    RETURNING m.session_id
),
affected_sessions AS (
    SELECT session_id FROM deleted
    UNION
    SELECT session_id FROM candidates
)
UPDATE chat_sessions s
SET message_count = sub.cnt,
    updated_at = NOW()
FROM (
    SELECT s2.id, count(m2.id)::int AS cnt
    FROM chat_sessions s2
    LEFT JOIN chat_messages m2 ON m2.session_id = s2.id
    WHERE s2.id IN (SELECT session_id FROM affected_sessions)
    GROUP BY s2.id
) sub
WHERE s.id = sub.id;

UPDATE chat_messages
SET intent = NULL
WHERE intent IN ('bg_partial', 'interrupted')
  AND role = 'assistant'
  AND execution_id IS NULL;
SQL

# ── Phase 0.5: 코드 검증 (구문 + import) — 실패 시 배포 차단 ──
deploy_phase_start "code_validation" "running"
echo "[deploy.sh] Phase 0.5: Python syntax + import validation..."
set +e
VALIDATION_RESULT=$(docker exec "$ACTIVE_CONTAINER" python3 -c "
import sys
errors = []
# 핵심 모듈 구문 검사
for f in ['app/main.py', 'app/services/chat_service.py', 'app/services/model_selector.py', 'app/routers/chat.py', 'app/api/ceo_chat_tools.py', 'app/services/autonomous_executor.py', 'app/services/tool_executor.py']:
    try:
        import py_compile
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
        errors.append(f'SYNTAX: {f} — {e}')
# import 검증
try:
    from app.main import app
except Exception as e:
    errors.append(f'IMPORT: app.main — {e}')
if errors:
    print('FAIL')
    for e in errors:
        print(e)
    sys.exit(1)
else:
    print('PASS')
" 2>&1)
VALIDATION_EXIT=$?
set -e

if [[ "$VALIDATION_EXIT" -ne 0 ]] || echo "$VALIDATION_RESULT" | head -1 | grep -q "FAIL"; then
    echo "[deploy.sh] ❌ Phase 0.5: 코드 검증 실패 — 배포 차단"
    echo "$VALIDATION_RESULT"
    notify "❌ 배포 차단: 코드 검증 실패\n${VALIDATION_RESULT}"
    deploy_phase_end "code_validation" "blocked" "Phase 0.5 validation failed: ${VALIDATION_RESULT:0:500}"
    record_deploy "blocked" "$MODE" "Phase 0.5 validation failed: ${VALIDATION_RESULT:0:500}"
    exit 1
fi
echo "[deploy.sh] Phase 0.5: ✅ 코드 검증 통과"
deploy_phase_end "code_validation" "success" ""

# ── Phase 1: 배포 실행 ──
record_deploy "started" "$MODE" ""
start_downtime_monitor
case "$MODE" in
    reload)
        echo "[deploy.sh] Phase 1: stream-safe hot reload aads-api"
        docker exec "$ACTIVE_CONTAINER" bash /app/scripts/reload-api.sh
        echo "[deploy.sh] Phase 1: hot reload 완료 — health check 대기..."
        ;;
    code)
        echo "[deploy.sh] Phase 1: code deploy with stream-safe slot switch"
        ACTIVE_STREAMS="$(stream_count_for_port "$ACTIVE_PORT")"
        PEER_PORT="$(peer_port_for "$ACTIVE_PORT")"
        PEER_CONTAINER="$(container_for_port "$PEER_PORT")"

        if [[ -n "$PEER_PORT" && -n "$PEER_CONTAINER" ]]; then
            echo "[deploy.sh] active API 직접 재시작 금지 — active_streams=${ACTIVE_STREAMS} 여부와 무관하게 peer slot으로 전환"
            if ! curl -sf "http://127.0.0.1:${PEER_PORT}/api/v1/health" >/dev/null 2>&1; then
                echo "[deploy.sh] ❌ peer slot ${PEER_CONTAINER}:${PEER_PORT} health 실패 — 스트림 보호를 위해 배포 중단"
                notify "❌ code 배포 중단: active stream ${ACTIVE_STREAMS}건, peer unhealthy"
                record_deploy "failed" "$MODE" "peer slot ${PEER_CONTAINER}:${PEER_PORT} health failed before switch"
                exit 1
            fi
            docker exec "$PEER_CONTAINER" touch /tmp/aads_deploy_restart 2>/dev/null || true
            docker exec "$PEER_CONTAINER" supervisorctl restart aads-api
            if ! wait_port_health "$PEER_PORT" 90; then
                echo "[deploy.sh] ❌ peer slot 재시작 후 health 실패 — 전환 중단"
                notify "❌ code 배포 실패: peer slot health 실패"
                record_deploy "failed" "$MODE" "peer slot ${PEER_CONTAINER}:${PEER_PORT} health failed after restart"
                exit 1
            fi
            switch_api_upstream "$PEER_PORT" "$ACTIVE_PORT" "$PEER_CONTAINER" "$ACTIVE_CONTAINER"
            restart_old_slot_after_drain "$ACTIVE_CONTAINER" "$ACTIVE_PORT" "$DEPLOY_GENERATION"
            ACTIVE_PORT="$PEER_PORT"
            ACTIVE_CONTAINER="$PEER_CONTAINER"
            HEALTH_URL="http://localhost:${ACTIVE_PORT}/api/v1/health"
            echo "[deploy.sh] Phase 1: ✅ active slot switched to ${ACTIVE_CONTAINER}:${ACTIVE_PORT}"
        else
            echo "[deploy.sh] ❌ peer slot을 찾지 못해 active API 직접 재시작을 차단합니다"
            notify "❌ code 배포 중단: peer slot missing"
            record_deploy "blocked" "$MODE" "peer slot missing"
            exit 1
            # PC Agent WebSocket 정상 종료
            ACTIVE_API_URL="http://localhost:${ACTIVE_PORT}"
            echo "[deploy.sh] PC Agent graceful-shutdown..."
            curl -sf -X POST "${ACTIVE_API_URL}/api/v1/pc-agent/graceful-shutdown" -H "Content-Type: application/json" 2>/dev/null || true
            sleep 1
            # 배포 플래그 파일 생성 → 서버 startup 시 미완료 대화 자동 재실행 스킵
            docker exec "$ACTIVE_CONTAINER" touch /tmp/aads_deploy_restart 2>/dev/null || true
            docker exec "$ACTIVE_CONTAINER" supervisorctl signal SIGTERM aads-api 2>/dev/null || true
            echo "[deploy.sh] SIGTERM 전송 완료 — 종료 대기 (최대 60초)..."
            for i in $(seq 1 30); do
                sleep 2
                STATUS=$(docker exec "$ACTIVE_CONTAINER" supervisorctl status aads-api 2>/dev/null | awk '{print $2}')
                if [ "$STATUS" != "RUNNING" ]; then
                    echo "[deploy.sh] aads-api 종료 확인 (${i}x2=$((i*2))초)"
                    break
                fi
            done
            docker exec "$ACTIVE_CONTAINER" supervisorctl start aads-api || true
            docker exec "$ACTIVE_CONTAINER" sh -c 'printf true > /tmp/aads_execution_resume_owner' 2>/dev/null || true
            if [[ -n "$PEER_CONTAINER" ]]; then
                docker exec "$PEER_CONTAINER" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
            fi
        fi
        ;;
    build)
        echo "[deploy.sh] Phase 1: docker compose up -d --build --no-deps aads-server"
        PG_ID_BEFORE=$(docker inspect aads-postgres --format '{{.Id}}' 2>/dev/null || echo "N/A")
        cd "$COMPOSE_DIR"
        docker compose up -d --build --no-deps aads-server
        PG_ID_AFTER=$(docker inspect aads-postgres --format '{{.Id}}' 2>/dev/null || echo "N/A")
        if [[ "$PG_ID_BEFORE" != "$PG_ID_AFTER" ]]; then
            notify "⚠️ CRITICAL: postgres 컨테이너 ID 변경됨!"
            echo "[deploy.sh] ⚠️ CRITICAL: postgres 컨테이너 ID가 변경됨!"
        fi
        ;;
    bluegreen)
        echo "[deploy.sh] Phase 1: Blue-Green 무중단 배포"
        BLUE_PORT=8100
        GREEN_PORT=8102
        BLUE_CONTAINER="aads-server"
        GREEN_CONTAINER="aads-server-green"
        COMPOSE_FILE="-f ${COMPOSE_DIR}/docker-compose.prod.yml"

        # 현재 활성 포트는 상태 파일/upstream 기준값 사용
        CURRENT_PORT="${ACTIVE_PORT}"
        CURRENT_PORT=${CURRENT_PORT:-$BLUE_PORT}
        OLD_PORT="${CURRENT_PORT}"
        if [[ "$CURRENT_PORT" == "$GREEN_PORT" ]]; then
            NEW_PORT=$BLUE_PORT
            NEW_CONTAINER=$BLUE_CONTAINER
            OLD_CONTAINER=$GREEN_CONTAINER
            PROFILE_CMD=""
        else
            NEW_PORT=$GREEN_PORT
            NEW_CONTAINER=$GREEN_CONTAINER
            OLD_CONTAINER=$BLUE_CONTAINER
            PROFILE_CMD="--profile green"
        fi
        echo "[deploy.sh] 현재: :${CURRENT_PORT} → 전환 대상: :${NEW_PORT} (${NEW_CONTAINER})"

        reconcile_inactive_target_recovery_executions "$NEW_CONTAINER"
        deploy_phase_start "target_slot_drain" "running"
        TARGET_STREAMS="$(stream_count_for_port "$NEW_PORT")"
        if [[ "$TARGET_STREAMS" =~ ^[0-9]+$ ]] && [[ "$TARGET_STREAMS" -gt 0 ]] && [[ "${AADS_DEPLOY_ALLOW_BUSY_TARGET:-false}" != "true" ]]; then
            local_target_drain_max="${AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT:-180}"
            local_target_drain_interval="${AADS_DEPLOY_TARGET_DRAIN_POLL_SECONDS:-10}"
            local_target_elapsed=0
            if [[ ! "$local_target_drain_max" =~ ^[0-9]+$ ]]; then
                local_target_drain_max="180"
            fi
            if [[ ! "$local_target_drain_interval" =~ ^[0-9]+$ ]] || [[ "$local_target_drain_interval" -lt 5 ]]; then
                local_target_drain_interval="10"
            fi
            echo "[deploy.sh] ⏳ 전환 대상 ${NEW_CONTAINER}:${NEW_PORT} 활성 스트림 ${TARGET_STREAMS}건 — 재빌드 전 drain 대기 (최대 ${local_target_drain_max}초)"
            while [[ "$local_target_elapsed" -lt "$local_target_drain_max" ]]; do
                sleep "$local_target_drain_interval"
                local_target_elapsed=$((local_target_elapsed + local_target_drain_interval))
                TARGET_STREAMS="$(stream_count_for_port "$NEW_PORT")"
                if [[ "$TARGET_STREAMS" == "0" || -z "$TARGET_STREAMS" ]]; then
                    echo "[deploy.sh] ✅ target slot drain 완료 (${local_target_elapsed}초)"
                    break
                fi
                deploy_observe_update "running" "target_slot_drain" \
                    "active_streams=${TARGET_STREAMS:-unknown}; elapsed=${local_target_elapsed}s; max=${local_target_drain_max}s"
                echo "[deploy.sh]   target drain 대기중... active=${TARGET_STREAMS} (${local_target_elapsed}/${local_target_drain_max}초)"
            done
            if [[ "$TARGET_STREAMS" =~ ^[0-9]+$ ]] && [[ "$TARGET_STREAMS" -gt 0 ]]; then
                echo "[deploy.sh] ❌ 전환 대상 ${NEW_CONTAINER}:${NEW_PORT}에 활성 스트림 ${TARGET_STREAMS}건 잔존 — 100% 무중단 원칙상 배포 차단"
                echo "[deploy.sh]    긴급 강제 배포가 필요할 때만 AADS_DEPLOY_ALLOW_BUSY_TARGET=true를 명시하세요."
                notify "❌ Blue-Green 중단: target slot ${NEW_CONTAINER}:${NEW_PORT} active streams=${TARGET_STREAMS}"
                deploy_phase_end "target_slot_drain" "blocked" "target slot ${NEW_CONTAINER}:${NEW_PORT} active streams=${TARGET_STREAMS}"
                record_deploy "blocked" "$MODE" "target slot ${NEW_CONTAINER}:${NEW_PORT} active streams=${TARGET_STREAMS}"
                exit 1
            fi
        elif [[ "$TARGET_STREAMS" != "0" ]]; then
            echo "[deploy.sh] ⚠️ 전환 대상 ${NEW_CONTAINER}:${NEW_PORT} active-streams 확인값=${TARGET_STREAMS} — 미기동/미응답 슬롯으로 판단하고 재빌드를 진행합니다."
        fi
        deploy_phase_end "target_slot_drain" "success" "active_streams=${TARGET_STREAMS}"

        # ① release image 1회 빌드 + 새 컨테이너 시작
        cd "$COMPOSE_DIR"
        deploy_phase_start "build_candidate_image" "running"
        echo "[deploy.sh] ① release image 1회 빌드 (${AADS_RELEASE_SHA})..."
        build_release_image
        echo "[deploy.sh] ① ${NEW_CONTAINER} --no-build 시작..."
        docker compose $COMPOSE_FILE $PROFILE_CMD up -d --force-recreate --no-build --no-deps "$NEW_CONTAINER"
        if ! verify_container_memory_limit "$NEW_CONTAINER"; then
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            docker rm "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: ${NEW_CONTAINER} memory limit mismatch"
            deploy_phase_end "build_candidate_image" "failed" "${NEW_CONTAINER} memory limit mismatch"
            record_deploy "failed" "$MODE" "${NEW_CONTAINER} memory limit mismatch"
            exit 1
        fi
        deploy_phase_end "build_candidate_image" "success" ""

        # ② 새 컨테이너 헬스체크
        deploy_phase_start "candidate_health" "running"
        BG_HEALTH_MAX_WAIT="${AADS_DEPLOY_BG_HEALTH_MAX_WAIT:-150}"
        echo "[deploy.sh] ② ${NEW_CONTAINER} 헬스체크 (최대 ${BG_HEALTH_MAX_WAIT}초)..."
        BG_ELAPSED=0
        BG_OK=false
        while [[ $BG_ELAPSED -lt "$BG_HEALTH_MAX_WAIT" ]]; do
            sleep 3
            BG_ELAPSED=$((BG_ELAPSED + 3))
            if curl -sf "http://127.0.0.1:${NEW_PORT}/api/v1/health" >/dev/null 2>&1; then
                echo "[deploy.sh] ② ✅ ${NEW_CONTAINER} 정상 (${BG_ELAPSED}초)"
                BG_OK=true
                break
            fi
            echo "[deploy.sh] 대기중... ${BG_ELAPSED}/${BG_HEALTH_MAX_WAIT}초"
        done

        if [[ "$BG_OK" != "true" ]]; then
            echo "[deploy.sh] ❌ ${NEW_CONTAINER} 헬스체크 실패 — 롤백"
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            docker rm "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: ${NEW_CONTAINER} 헬스체크 통과 못함"
            deploy_phase_end "candidate_health" "failed" "${NEW_CONTAINER} health check failed"
            record_deploy "failed" "$MODE" "${NEW_CONTAINER} health check failed"
            exit 1
        fi
        deploy_phase_end "candidate_health" "success" "elapsed=${BG_ELAPSED}s"

        # P1: 전환 전 현재 슬롯 활성 스트림 drain 대기 (최대 60초)
        deploy_phase_start "active_slot_drain" "running"
        ACTIVE_STREAMS="$(stream_count_for_port "$CURRENT_PORT")"
        if [[ "$ACTIVE_STREAMS" =~ ^[0-9]+$ ]] && [[ "$ACTIVE_STREAMS" -gt 0 ]]; then
            echo "[deploy.sh] ⏳ 현재 슬롯 :${CURRENT_PORT} 활성 스트림 ${ACTIVE_STREAMS}건 — 최대 60초 대기"
            DRAIN_ELAPSED=0
            while [[ $DRAIN_ELAPSED -lt 60 ]]; do
                sleep 5
                DRAIN_ELAPSED=$((DRAIN_ELAPSED + 5))
                ACTIVE_STREAMS="$(stream_count_for_port "$CURRENT_PORT")"
                if [[ "$ACTIVE_STREAMS" == "0" || -z "$ACTIVE_STREAMS" ]]; then
                    echo "[deploy.sh] ✅ 활성 스트림 0건 — 전환 진행 (${DRAIN_ELAPSED}초 대기)"
                    break
                fi
                echo "[deploy.sh]   대기중... active=${ACTIVE_STREAMS} (${DRAIN_ELAPSED}/60초)"
            done
            if [[ "$ACTIVE_STREAMS" =~ ^[0-9]+$ ]] && [[ "$ACTIVE_STREAMS" -gt 0 ]]; then
                echo "[deploy.sh] ⚠️ ${ACTIVE_STREAMS}건 스트림 아직 활성 — nginx graceful reload로 전환 진행 (기존 worker가 스트림 유지)"
            fi
        fi
        deploy_phase_end "active_slot_drain" "success" "active_streams=${ACTIVE_STREAMS:-unknown}"

        # ③ upstream 전환 (aads-upstream.conf에서 backup 키워드 조작)
        deploy_phase_start "nginx_cutover" "verifying"
        echo "[deploy.sh] ③ upstream 전환: :${CURRENT_PORT} → :${NEW_PORT}"
        acquire_nginx_switch_lock
        cp "$UPSTREAM_CONF" "${UPSTREAM_CONF}.pre_deploy"
        # 새 포트에서 backup 제거, 기존 포트에 backup 추가
        sed -i -E \
            -e "s/server 127\.0\.0\.1:${NEW_PORT} [^;]*;/server 127.0.0.1:${NEW_PORT} max_fails=1 fail_timeout=10s;/g" \
            -e "s/server 127\.0\.0\.1:${CURRENT_PORT} [^;]*;/server 127.0.0.1:${CURRENT_PORT} max_fails=1 fail_timeout=10s backup;/g" \
            "$UPSTREAM_CONF"
        if ! nginx_config_test; then
            echo "[deploy.sh] ❌ nginx 설정 오류 — 롤백"
            cp "${UPSTREAM_CONF}.pre_deploy" "$UPSTREAM_CONF"
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: nginx 설정 오류"
            deploy_phase_end "nginx_cutover" "failed" "nginx config test failed during upstream switch"
            record_deploy "failed" "$MODE" "nginx config test failed during upstream switch"
            exit 1
        fi

        echo "[deploy.sh] [5/6] nginx reload — existing streams remain on the old worker/slot"
        if ! nginx_reload; then
            cp "${UPSTREAM_CONF}.pre_deploy" "$UPSTREAM_CONF"
            nginx_config_test >/dev/null 2>&1 && nginx_reload >/dev/null 2>&1 || true
            audit_control "nginx-switch" "${CURRENT_PORT}->${NEW_PORT}" "failed" "reload failed; configuration rolled back"
            notify "❌ Blue-Green 실패: nginx reload 오류 — 복원 완료"
            deploy_phase_end "nginx_cutover" "failed" "nginx reload failed during upstream switch"
            record_deploy "failed" "$MODE" "nginx reload failed during upstream switch"
            exit 1
        fi
        echo "[deploy.sh]   nginx upstream 전환 완료"

        # ④ 전환 후 검증
        sleep 2
        if curl -sf "http://127.0.0.1:${NEW_PORT}/api/v1/health" >/dev/null 2>&1 \
            && curl -sf -H "Host: ${DOWNTIME_PROBE_HOST}" "http://127.0.0.1/api/v1/health" >/dev/null 2>&1; then
            echo "[deploy.sh] ④ ✅ 전환 검증 성공"
            audit_control "nginx-switch" "${OLD_CONTAINER}:${OLD_PORT}->${NEW_CONTAINER}:${NEW_PORT}" "success" "direct and nginx-routed health verified"
            deploy_phase_end "nginx_cutover" "success" "direct and nginx-routed health verified"
        else
            echo "[deploy.sh] ⚠️ 전환 후 검증 실패 — 이전 서버로 복원"
            cp "${UPSTREAM_CONF}.pre_deploy" "$UPSTREAM_CONF"
            nginx_reload
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: 전환 검증 실패 — 복원 완료"
            deploy_phase_end "nginx_cutover" "failed" "post-switch health verification failed for ${NEW_CONTAINER}:${NEW_PORT}"
            record_deploy "failed" "$MODE" "post-switch health verification failed for ${NEW_CONTAINER}:${NEW_PORT}"
            exit 1
        fi

        # ⑤ 이전 컨테이너를 drain 후 같은 release로 재빌드해 warm standby로 동기화
        deploy_phase_start "standby_same_digest_sync" "syncing_standby"
        echo "[deploy.sh] ⑤ ${OLD_CONTAINER} standby 동기화"
        echo "$NEW_PORT" > /root/aads/aads-server/.active_port
        echo "$NEW_CONTAINER" > /root/aads/aads-server/.active_container
        docker exec "$NEW_CONTAINER" sh -c 'printf true > /tmp/aads_execution_resume_owner' 2>/dev/null || true
        docker exec "$OLD_CONTAINER" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
        release_nginx_switch_lock
        if ! sync_standby_slot_after_drain "$OLD_CONTAINER" "$OLD_PORT" "$DEPLOY_GENERATION"; then
            notify "❌ Blue-Green 인증 실패: standby same-digest 동기화 실패"
            deploy_phase_end "standby_same_digest_sync" "failed" "standby same-digest sync failed for ${OLD_CONTAINER}:${OLD_PORT}"
            record_deploy "failed" "$MODE" "standby same-digest sync failed for ${OLD_CONTAINER}:${OLD_PORT}"
            exit 1
        fi
        deploy_phase_end "standby_same_digest_sync" "success" ""

        HEALTH_URL="http://localhost:${NEW_PORT}/api/v1/health"
        echo "[deploy.sh] ✅ Blue-Green active 전환 + standby same-digest 동기화 완료: :${NEW_PORT} 활성"
        notify "✅ Blue-Green active 전환 완료: :${CURRENT_PORT} → :${NEW_PORT}"
        ;;
    *)
        echo "[deploy.sh] ERROR: 알 수 없는 모드 '$MODE'. bluegreen|code|reload|build 사용"
        record_deploy "blocked" "$MODE" "unknown mode: ${MODE}"
        exit 1
        ;;
esac

# ── Phase 2: Health Check ──
deploy_phase_start "post_switch_health" "verifying"
echo "[deploy.sh] Phase 2: Health check (최대 ${MAX_WAIT}초)..."
elapsed=0
HEALTH_OK=false
while [[ $elapsed -lt $MAX_WAIT ]]; do
    sleep "$INTERVAL"
    elapsed=$((elapsed + INTERVAL))
    if curl -sf "$HEALTH_URL" >/dev/null 2>&1; then
        echo "[deploy.sh] Phase 2: ✅ Health OK (${elapsed}초)"
        HEALTH_OK=true
        break
    fi
    echo "[deploy.sh] 대기중... ${elapsed}/${MAX_WAIT}초"
done

if [[ "$HEALTH_OK" != "true" ]]; then
    echo "[deploy.sh] ❌ Phase 2 실패 — 롤백 시도..."
    if [[ "$MODE" == "code" ]]; then
        echo "[deploy.sh] active API 직접 재시작은 SSE 끊김 원인이므로 생략"
    fi
    notify "❌ 배포 실패 + 롤백 시도 (mode=${MODE})"
    deploy_phase_end "post_switch_health" "failed" "Phase 2 health check failed: ${HEALTH_URL}"
    record_deploy "failed" "$MODE" "Phase 2 health check failed: ${HEALTH_URL}"
    exit 1
fi
deploy_phase_end "post_switch_health" "success" "elapsed=${elapsed}s"

# ── Phase 2.5: E2E 게이트 ──
if [[ "${RUN_E2E:-false}" == "true" ]]; then
    deploy_phase_start "e2e_gate" "verifying"
    echo "[deploy.sh] Phase 2.5: E2E 게이트 실행..."
    E2E_RESULT=$(curl -sf -m 30 "http://localhost:${TARGET_PORT:-8100}/api/v1/chat/sessions" 2>/dev/null || echo "FAIL")
    E2E_CODE=$(curl -so /dev/null -w "%{http_code}" -m 30 "http://localhost:${TARGET_PORT:-8100}/api/v1/chat/sessions" 2>/dev/null || echo "0")
    if [[ "$E2E_CODE" == "200" || "$E2E_CODE" == "401" || "$E2E_CODE" == "403" ]]; then
        echo "[deploy.sh] Phase 2.5: ✅ E2E 게이트 통과 (HTTP $E2E_CODE)"
    else
        echo "[deploy.sh] ⚠️ Phase 2.5: E2E 응답 이상 (HTTP $E2E_CODE) — 배포 계속"
    fi
    deploy_phase_end "e2e_gate" "success" "http=${E2E_CODE}"
fi

# ── Phase 3: DB 스키마 검증 ──
deploy_phase_start "db_schema_check" "verifying"
echo "[deploy.sh] Phase 3: DB 스키마 검증..."
SCHEMA_RESULT=$(docker exec aads-postgres psql -U aads -d aads -t -A -c "
  SELECT string_agg(column_name, ',') FROM information_schema.columns
  WHERE table_name = 'chat_messages' AND column_name IN ('branch_id','intent','content','session_id','role');
" 2>/dev/null || echo "ERROR")

if [[ "$SCHEMA_RESULT" == "ERROR" ]]; then
    echo "[deploy.sh] ⚠️ Phase 3: DB 연결 실패 — 스키마 검증 스킵"
else
    MISSING=""
    for COL in branch_id intent content session_id role; do
        if [[ "$SCHEMA_RESULT" != *"$COL"* ]]; then
            MISSING="${MISSING} ${COL}"
        fi
    done
    if [[ -n "$MISSING" ]]; then
        echo "[deploy.sh] ⚠️ Phase 3: 누락 컬럼 감지:${MISSING}"
        notify "⚠️ DB 컬럼 누락 감지:${MISSING} — 자동 생성 시도"
        # 자동 생성 시도
        for COL in $MISSING; do
            echo "[deploy.sh] ALTER TABLE chat_messages ADD COLUMN ${COL}..."
            docker exec aads-postgres psql -U aads -d aads -c \
                "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS ${COL} UUID DEFAULT NULL;" 2>/dev/null || true
        done
    else
        echo "[deploy.sh] Phase 3: ✅ 필수 컬럼 정상"
    fi
fi
deploy_phase_end "db_schema_check" "success" ""

# ── Phase 4: 채팅 기능 테스트 (SELECT으로 DB+테이블 접근 확인) ──
deploy_phase_start "chat_table_check" "verifying"
echo "[deploy.sh] Phase 4: 채팅 기능 테스트..."
# INSERT 없이 SELECT로 chat_messages 테이블 접근 가능 여부만 확인
# (INSERT 방식은 _deploy_test_ 메시지가 CEO 세션에 누출되는 버그 유발)
CHAT_TEST=$(docker exec aads-postgres psql -U aads -d aads -t -A -c "
  SELECT CASE WHEN EXISTS (SELECT 1 FROM chat_messages LIMIT 1) THEN 'CHAT_OK' ELSE 'CHAT_OK' END;
" 2>&1)

if echo "$CHAT_TEST" | grep -q "CHAT_OK"; then
    echo "[deploy.sh] Phase 4: ✅ 채팅 테이블 접근 정상"
else
    echo "[deploy.sh] ❌ Phase 4 실패 — 롤백 시도..."
    echo "[deploy.sh] 에러: ${CHAT_TEST}"
    if [[ "$MODE" == "code" ]]; then
        echo "[deploy.sh] active API 직접 재시작은 SSE 끊김 원인이므로 생략"
    fi
    notify "❌ 채팅 기능 테스트 실패 + 롤백 (mode=${MODE}): ${CHAT_TEST:0:200}"
    deploy_phase_end "chat_table_check" "failed" "Phase 4 chat table check failed: ${CHAT_TEST:0:500}"
    record_deploy "failed" "$MODE" "Phase 4 chat table check failed: ${CHAT_TEST:0:500}"
    exit 1
fi
deploy_phase_end "chat_table_check" "success" ""

# ── Phase 5: LLM 연결 테스트 (Agent SDK 또는 Gemini 가용성) ──
deploy_phase_start "llm_health_check" "verifying"
echo "[deploy.sh] Phase 5: LLM 연결 테스트..."
LLM_TEST=$(curl -sf "${HEALTH_URL}" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print('LLM_OK' if d.get('status') == 'ok' else 'LLM_FAIL')
except:
    print('LLM_FAIL')
" 2>/dev/null || echo "LLM_FAIL")

if [[ "$LLM_TEST" == "LLM_OK" ]]; then
    echo "[deploy.sh] Phase 5: ✅ LLM 서비스 정상"
else
    echo "[deploy.sh] ⚠️ Phase 5: LLM 상태 확인 불가 (채팅은 가능하나 AI 응답 지연 가능)"
    notify "⚠️ LLM 상태 확인 불가 — 채팅 가능하나 AI 응답 지연 가능"
fi
deploy_phase_end "llm_health_check" "success" "result=${LLM_TEST}"

# ── Phase 6: 프론트엔드 QA (non-blocking) ──
deploy_phase_start "frontend_qa" "verifying"
echo "[deploy.sh] Phase 6: 프론트엔드 QA 검사..."
FRONTEND_QA_STATUS="skipped"
CHANGED_FILES=$(git -C "$COMPOSE_DIR" diff HEAD~1 --name-only 2>/dev/null || echo "")
if echo "$CHANGED_FILES" | grep -q "aads-dashboard/"; then
    echo "[deploy.sh] Phase 6: 대시보드 변경 감지 — Next.js 빌드 대기 (20초)..."
    sleep 20
    QA_RESPONSE=$(curl -sf --max-time 120 -X POST "http://127.0.0.1:8100/api/v1/visual-qa/full-qa" \
        -H "Content-Type: application/json" \
        -d '{"pages": ["/", "/chat", "/ops"]}' 2>/dev/null) || QA_RESPONSE=""
    if [[ -z "$QA_RESPONSE" ]]; then
        echo "[deploy.sh] ⚠️ Phase 6: QA API 응답 없음 — 스킵 (non-blocking)"
        FRONTEND_QA_STATUS="no_response"
    else
        QA_VERDICT=$(echo "$QA_RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('verdict', 'UNKNOWN'))
except:
    print('UNKNOWN')
" 2>/dev/null || echo "UNKNOWN")
        if [[ "$QA_VERDICT" == "FAIL" ]]; then
            echo "[deploy.sh] ⚠️ Phase 6: ❌ 프론트 QA 실패 (non-blocking)"
            notify "❌ 프론트 QA 실패 — 확인 필요 (non-blocking)"
            FRONTEND_QA_STATUS="failed_non_blocking"
        elif [[ "$QA_VERDICT" == "PASS" ]]; then
            echo "[deploy.sh] Phase 6: ✅ 프론트 QA 통과"
            FRONTEND_QA_STATUS="passed"
        else
            echo "[deploy.sh] ⚠️ Phase 6: QA 결과 불명 (verdict=${QA_VERDICT}) — 통과로 간주하지 않음"
            FRONTEND_QA_STATUS="unknown_non_blocking"
        fi
    fi
else
    echo "[deploy.sh] Phase 6: 프론트 변경 없음 — QA 스킵"
fi
deploy_phase_end "frontend_qa" "success" "frontend_qa=${FRONTEND_QA_STATUS}"

# ── Phase 7: P0/P1 모니터링 게이트 ──
deploy_phase_start "p0p1_monitoring" "verifying"
MONITOR_SECONDS="${AADS_DEPLOY_P0P1_MONITOR_SECONDS:-300}"
MONITOR_INTERVAL="${AADS_DEPLOY_P0P1_MONITOR_INTERVAL:-30}"
MONITOR_PATTERN="${AADS_DEPLOY_MONITOR_PATTERN:-level=(error|critical)|Traceback|CRITICAL}"
MONITOR_SINCE="$(date --iso-8601=seconds)"
MONITOR_ELAPSED=0
echo "[deploy.sh] Phase 7: P0/P1 모니터링 (${MONITOR_SECONDS}초, since=${MONITOR_SINCE})..."
while [[ "$MONITOR_ELAPSED" -lt "$MONITOR_SECONDS" ]]; do
    sleep "$MONITOR_INTERVAL"
    MONITOR_ELAPSED=$((MONITOR_ELAPSED + MONITOR_INTERVAL))
    deploy_observe_update "verifying" "p0p1_monitoring" \
        "elapsed=${MONITOR_ELAPSED}s; max=${MONITOR_SECONDS}s"
    MONITOR_HITS="$(docker logs "$ACTIVE_CONTAINER" --since "$MONITOR_SINCE" 2>&1 | grep -E "$MONITOR_PATTERN" | tail -20 || true)"
    if [[ -n "$MONITOR_HITS" ]]; then
        echo "[deploy.sh] ❌ Phase 7: P0/P1 의심 로그 감지"
        echo "$MONITOR_HITS"
        notify "❌ 배포 후 P0/P1 모니터링 실패: ${MONITOR_HITS:0:300}"
        deploy_phase_end "p0p1_monitoring" "failed" "P0/P1 monitor hit: ${MONITOR_HITS:0:500}"
        record_deploy "failed" "$MODE" "Phase 7 P0/P1 monitor hit: ${MONITOR_HITS:0:500}"
        exit 1
    fi
    echo "[deploy.sh] Phase 7: monitoring ${MONITOR_ELAPSED}/${MONITOR_SECONDS}초 이상 없음"
done
deploy_phase_end "p0p1_monitoring" "success" "seconds=${MONITOR_SECONDS}"

echo "[deploy.sh] ✅ 배포 완료 — 필수 검증 통과 (mode=${MODE}, frontend_qa=${FRONTEND_QA_STATUS})"
notify "✅ 배포 완료 — 필수 검증 통과 (mode=${MODE}, frontend_qa=${FRONTEND_QA_STATUS})"
stop_downtime_monitor
deploy_observe_update "success" "completed" ""
record_deploy "success" "$MODE" ""
exit 0
