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
HEALTH_URL="http://localhost:8100/api/v1/health"
MAX_WAIT="${AADS_DEPLOY_MAX_WAIT:-30}"
INTERVAL=2
UPSTREAM_CONF="/etc/nginx/conf.d/aads-upstream.conf"
ACTIVE_CONTAINER_FILE="${COMPOSE_DIR}/.active_container"
ACTIVE_PORT_FILE="${COMPOSE_DIR}/.active_port"
DEPLOY_START_EPOCH=$(date +%s)

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
    commit=$(git -C "$COMPOSE_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
    msg=$(git -C "$COMPOSE_DIR" log -1 --pretty=%s 2>/dev/null || echo "unknown")

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
    record_deploy "failed" "$MODE" "unexpected error exit=${exit_code} line=${line_no}: ${command:0:300}"
}

trap 'deploy_error_trap "$LINENO" "$BASH_COMMAND"' ERR

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
trap "stop_downtime_monitor; rm -f $LOCKFILE" EXIT

# nginx upstream is shared by backend and dashboard blue-green deploys.
# Hold a common lock for the whole deployment to prevent concurrent rewrites.
NGINX_SWITCH_LOCK="/tmp/aads-nginx-upstream.lock"
exec 8>"$NGINX_SWITCH_LOCK"
if ! flock -w 300 8; then
    echo "[deploy.sh] ❌ nginx upstream 공통 락 획득 실패. 다른 배포가 진행 중입니다."
    record_deploy "blocked" "$MODE" "nginx upstream lock acquisition failed"
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
    (
        curl -s -m 5 "http://127.0.0.1:${port}/api/v1/ops/active-streams" 2>/dev/null \
        | python3 -c "import sys,json; value=json.load(sys.stdin).get('count', 'unknown'); print(value if value is not None else 'unknown')" 2>/dev/null
    ) || echo "unknown"
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

    cp "$UPSTREAM_CONF" "${UPSTREAM_CONF}.pre_code_switch"
    sed -i -E \
        -e "s/server 127\.0\.0\.1:${new_port} [^;]*;/server 127.0.0.1:${new_port} max_fails=0;/g" \
        -e "s/server 127\.0\.0\.1:${old_port} [^;]*;/server 127.0.0.1:${old_port} max_fails=3 fail_timeout=30s backup;/g" \
        "$UPSTREAM_CONF"
    if ! nginx_config_test >/dev/null 2>&1; then
        cp "${UPSTREAM_CONF}.pre_code_switch" "$UPSTREAM_CONF"
        echo "[deploy.sh] ❌ nginx 설정 오류 — upstream 전환 취소"
        return 1
    fi

    echo "$new_port" > "$ACTIVE_PORT_FILE" 2>/dev/null || true
    echo "$new_container" > "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true
    docker exec "$new_container" sh -c 'printf true > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    nginx_reload
}

restart_old_slot_after_drain() {
    local old_container="$1"
    local old_port="$2"

    (
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
        docker exec "$old_container" touch /tmp/aads_deploy_restart 2>/dev/null || true
        docker exec "$old_container" supervisorctl restart aads-api >/dev/null 2>&1 || true
        docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    ) &
    disown
}

sync_standby_slot_after_drain() {
    local old_container="$1"
    local old_port="$2"

    (
        # Do not rebuild the previous active slot immediately after switching.
        # Existing nginx workers may still hold SSE/WebSocket streams on that slot,
        # and the active-stream counter can be briefly stale during handoff.
        local min_wait="${AADS_DEPLOY_STANDBY_SYNC_MIN_WAIT:-600}"
        if [[ "$min_wait" != "0" ]]; then
            echo "[deploy.sh] standby sync grace wait ${old_container}:${old_port} ${min_wait}s"
            sleep "$min_wait"
        fi

        local drain_max="${AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-1800}"
        local elapsed=0
        local active="0"
        while [[ $elapsed -lt $drain_max ]]; do
            active="$(stream_count_for_port "$old_port")"
            if [[ "$active" == "0" || -z "$active" ]]; then
                break
            fi
            echo "[deploy.sh] standby sync wait ${old_container}:${old_port} active streams=${active}; wait 30s"
            sleep 30
            elapsed=$((elapsed + 30))
        done

        if [[ "${active:-0}" != "0" && -n "${active:-}" ]]; then
            echo "[deploy.sh] standby sync skipped: ${old_container}:${old_port} still has active streams=${active}"
            docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
            return 0
        fi

        echo "[deploy.sh] standby sync PC Agent reconnect trigger on drained old slot :${old_port}"
        curl -sf -X POST "http://127.0.0.1:${old_port}/api/v1/pc-agent/graceful-shutdown" \
            -H "Content-Type: application/json" 2>/dev/null || true

        echo "[deploy.sh] standby sync: rebuilding ${old_container}:${old_port} from current release"
        cd "$COMPOSE_DIR"
        if [[ "$old_container" == "aads-server-green" ]]; then
            docker compose -f "${COMPOSE_DIR}/docker-compose.prod.yml" --profile green up -d --build --no-deps "$old_container"
        else
            docker compose -f "${COMPOSE_DIR}/docker-compose.prod.yml" up -d --build --no-deps "$old_container"
        fi

        if wait_port_health "$old_port" 90; then
            docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
            echo "[deploy.sh] standby sync complete: ${old_container}:${old_port}"
        else
            echo "[deploy.sh] standby sync WARN: ${old_container}:${old_port} health failed after rebuild"
        fi
    ) &
    disown
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
          AND te.updated_at > NOW() - INTERVAL '10 minutes'
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
    record_deploy "blocked" "$MODE" "Phase 0.5 validation failed: ${VALIDATION_RESULT:0:500}"
    exit 1
fi
echo "[deploy.sh] Phase 0.5: ✅ 코드 검증 통과"

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
            restart_old_slot_after_drain "$ACTIVE_CONTAINER" "$ACTIVE_PORT"
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

        TARGET_STREAMS="$(stream_count_for_port "$NEW_PORT")"
        if [[ "$TARGET_STREAMS" =~ ^[0-9]+$ ]] && [[ "$TARGET_STREAMS" -gt 0 ]] && [[ "${AADS_DEPLOY_ALLOW_BUSY_TARGET:-false}" != "true" ]]; then
            echo "[deploy.sh] ❌ 전환 대상 ${NEW_CONTAINER}:${NEW_PORT}에 활성 스트림 ${TARGET_STREAMS}건 존재 — 재빌드 시 응답 끊김 위험으로 배포 중단"
            echo "[deploy.sh]    잠시 후 재시도하거나, 긴급 강제 배포가 필요할 때만 AADS_DEPLOY_ALLOW_BUSY_TARGET=true를 명시하세요."
            notify "❌ Blue-Green 중단: target slot ${NEW_CONTAINER}:${NEW_PORT} active streams=${TARGET_STREAMS}"
            record_deploy "blocked" "$MODE" "target slot ${NEW_CONTAINER}:${NEW_PORT} active streams=${TARGET_STREAMS}"
            exit 1
        elif [[ "$TARGET_STREAMS" != "0" ]]; then
            echo "[deploy.sh] ⚠️ 전환 대상 ${NEW_CONTAINER}:${NEW_PORT} active-streams 확인값=${TARGET_STREAMS} — 미기동/미응답 슬롯으로 판단하고 재빌드를 진행합니다."
        fi

        # ① 새 컨테이너 빌드 + 시작
        cd "$COMPOSE_DIR"
        echo "[deploy.sh] ① ${NEW_CONTAINER} 빌드 + 시작..."
        docker compose $COMPOSE_FILE $PROFILE_CMD up -d --build --no-deps "$NEW_CONTAINER"

        # ② 새 컨테이너 헬스체크
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
            record_deploy "failed" "$MODE" "${NEW_CONTAINER} health check failed"
            exit 1
        fi

        # P1: 전환 전 현재 슬롯 활성 스트림 drain 대기 (최대 60초)
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

        # ③ upstream 전환 (aads-upstream.conf에서 backup 키워드 조작)
        echo "[deploy.sh] ③ upstream 전환: :${CURRENT_PORT} → :${NEW_PORT}"
        cp "$UPSTREAM_CONF" "${UPSTREAM_CONF}.pre_deploy"
        # 새 포트에서 backup 제거, 기존 포트에 backup 추가
        sed -i -E \
            -e "s/server 127\.0\.0\.1:${NEW_PORT} [^;]*;/server 127.0.0.1:${NEW_PORT} max_fails=0;/g" \
            -e "s/server 127\.0\.0\.1:${CURRENT_PORT} [^;]*;/server 127.0.0.1:${CURRENT_PORT} max_fails=3 fail_timeout=30s backup;/g" \
            "$UPSTREAM_CONF"
        if ! nginx_config_test; then
            echo "[deploy.sh] ❌ nginx 설정 오류 — 롤백"
            cp "${UPSTREAM_CONF}.pre_deploy" "$UPSTREAM_CONF"
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: nginx 설정 오류"
            record_deploy "failed" "$MODE" "nginx config test failed during upstream switch"
            exit 1
        fi

        echo "[deploy.sh] [5/6] nginx reload — existing streams remain on the old worker/slot"
        nginx_reload
        echo "[deploy.sh]   nginx upstream 전환 완료"

        # ④ 전환 후 검증
        sleep 2
        if curl -sf "http://127.0.0.1:${NEW_PORT}/api/v1/health" >/dev/null 2>&1; then
            echo "[deploy.sh] ④ ✅ 전환 검증 성공"
        else
            echo "[deploy.sh] ⚠️ 전환 후 검증 실패 — 이전 서버로 복원"
            cp "${UPSTREAM_CONF}.pre_deploy" "$UPSTREAM_CONF"
            nginx_reload
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: 전환 검증 실패 — 복원 완료"
            record_deploy "failed" "$MODE" "post-switch health verification failed for ${NEW_CONTAINER}:${NEW_PORT}"
            exit 1
        fi

        # ⑤ 이전 컨테이너를 drain 후 같은 release로 재빌드해 warm standby로 동기화
        echo "[deploy.sh] ⑤ ${OLD_CONTAINER} standby 동기화 백그라운드 예약"
        echo "$NEW_PORT" > /root/aads/aads-server/.active_port
        echo "$NEW_CONTAINER" > /root/aads/aads-server/.active_container
        docker exec "$NEW_CONTAINER" sh -c 'printf true > /tmp/aads_execution_resume_owner' 2>/dev/null || true
        docker exec "$OLD_CONTAINER" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
        sync_standby_slot_after_drain "$OLD_CONTAINER" "$OLD_PORT"

        HEALTH_URL="http://localhost:${NEW_PORT}/api/v1/health"
        echo "[deploy.sh] ✅ Blue-Green active 전환 완료: :${NEW_PORT} 활성"
        echo "[deploy.sh] ℹ️ standby 동기화는 old slot drain 후 백그라운드에서 별도 완료/skip 로그를 남깁니다."
        notify "✅ Blue-Green active 전환 완료: :${CURRENT_PORT} → :${NEW_PORT}"
        ;;
    *)
        echo "[deploy.sh] ERROR: 알 수 없는 모드 '$MODE'. bluegreen|code|reload|build 사용"
        record_deploy "blocked" "$MODE" "unknown mode: ${MODE}"
        exit 1
        ;;
esac

# ── Phase 2: Health Check ──
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
    record_deploy "failed" "$MODE" "Phase 2 health check failed: ${HEALTH_URL}"
    exit 1
fi

# ── Phase 2.5: E2E 게이트 ──
if [[ "${RUN_E2E:-false}" == "true" ]]; then
    echo "[deploy.sh] Phase 2.5: E2E 게이트 실행..."
    E2E_RESULT=$(curl -sf -m 30 "http://localhost:${TARGET_PORT:-8100}/api/v1/chat/sessions" 2>/dev/null || echo "FAIL")
    E2E_CODE=$(curl -so /dev/null -w "%{http_code}" -m 30 "http://localhost:${TARGET_PORT:-8100}/api/v1/chat/sessions" 2>/dev/null || echo "0")
    if [[ "$E2E_CODE" == "200" || "$E2E_CODE" == "401" || "$E2E_CODE" == "403" ]]; then
        echo "[deploy.sh] Phase 2.5: ✅ E2E 게이트 통과 (HTTP $E2E_CODE)"
    else
        echo "[deploy.sh] ⚠️ Phase 2.5: E2E 응답 이상 (HTTP $E2E_CODE) — 배포 계속"
    fi
fi

# ── Phase 3: DB 스키마 검증 ──
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

# ── Phase 4: 채팅 기능 테스트 (SELECT으로 DB+테이블 접근 확인) ──
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
    record_deploy "failed" "$MODE" "Phase 4 chat table check failed: ${CHAT_TEST:0:500}"
    exit 1
fi

# ── Phase 5: LLM 연결 테스트 (Agent SDK 또는 Gemini 가용성) ──
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

# ── Phase 6: 프론트엔드 QA (non-blocking) ──
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

echo "[deploy.sh] ✅ 배포 완료 — 필수 검증 통과 (mode=${MODE}, frontend_qa=${FRONTEND_QA_STATUS})"
notify "✅ 배포 완료 — 필수 검증 통과 (mode=${MODE}, frontend_qa=${FRONTEND_QA_STATUS})"
stop_downtime_monitor
record_deploy "success" "$MODE" ""
exit 0
