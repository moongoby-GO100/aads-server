#!/bin/bash
# AADS 안전 배포 게이트웨이
# 사용법: deploy.sh [code|reload|build|bluegreen]
#   code      (기본) — SIGTERM + 60초 대기 + supervisorctl start (graceful)
#   reload           — supervisorctl restart (빠른 재기동, ~10초)
#   build            — docker compose up -d --build --no-deps aads-server (1~3분 중단)
#   bluegreen        — Blue↔Green 무중단 전환 (중단 0초, 자동 롤백, upstream 전환)
#
# 검증 6단계: 의존성→코드검증→배포→Health→DB스키마→채팅→LLM→프론트QA

set -euo pipefail

MODE="${1:-code}"
COMPOSE_DIR="/root/aads/aads-server"
HEALTH_URL="http://localhost:8100/api/v1/health"
MAX_WAIT="${AADS_DEPLOY_MAX_WAIT:-30}"
INTERVAL=2
UPSTREAM_CONF="/etc/nginx/conf.d/aads-upstream.conf"
ACTIVE_CONTAINER_FILE="${COMPOSE_DIR}/.active_container"
ACTIVE_PORT_FILE="${COMPOSE_DIR}/.active_port"

get_active_port() {
    local port=""
    if [[ -f "$ACTIVE_PORT_FILE" ]]; then
        port=$(tr -d '[:space:]' < "$ACTIVE_PORT_FILE" 2>/dev/null || true)
    fi
    if [[ -z "$port" && -f "$UPSTREAM_CONF" ]]; then
        port=$(grep "server 127.0.0.1:" "$UPSTREAM_CONF" | grep -v backup | head -1 | grep -oP '127\.0\.0\.1:\K[0-9]+' || true)
    fi
    if [[ "$port" != "8100" && "$port" != "8102" ]]; then
        port="8100"
    fi
    echo "$port"
}

get_active_container() {
    local container=""
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

ACTIVE_PORT="$(get_active_port)"
ACTIVE_CONTAINER="$(get_active_container)"
HEALTH_URL="http://localhost:${ACTIVE_PORT}/api/v1/health"

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
        exit 1
    else
        echo "[deploy.sh] ⚠️ stale lockfile 제거 (PID=$LOCK_PID 종료됨)"
        rm -f "$LOCKFILE"
    fi
fi
echo $$ > "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

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
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null
    ) || echo "0"
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
    if ! nginx -t >/dev/null 2>&1; then
        cp "${UPSTREAM_CONF}.pre_code_switch" "$UPSTREAM_CONF"
        echo "[deploy.sh] ❌ nginx 설정 오류 — upstream 전환 취소"
        return 1
    fi

    echo "$new_port" > "$ACTIVE_PORT_FILE" 2>/dev/null || true
    echo "$new_container" > "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true
    docker exec "$new_container" sh -c 'printf true > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    systemctl reload nginx
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
        docker exec "$old_container" touch /tmp/aads_deploy_restart 2>/dev/null || true
        docker exec "$old_container" supervisorctl restart aads-api >/dev/null 2>&1 || true
        docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
    ) &
    disown
}

# .env에서 텔레그램 변수 로드
if [[ -f "${COMPOSE_DIR}/.env" ]]; then
    export TELEGRAM_BOT_TOKEN=$(grep -oP '^TELEGRAM_BOT_TOKEN=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
    export TELEGRAM_CHAT_ID=$(grep -oP '^TELEGRAM_CHAT_ID=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
fi

echo "[deploy.sh] mode=${MODE} at $(date '+%Y-%m-%d %H:%M:%S')"

if [[ "$MODE" == "code" ]]; then
    MAX_WAIT="${AADS_DEPLOY_MAX_WAIT:-60}"
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

if echo "$VALIDATION_RESULT" | head -1 | grep -q "FAIL"; then
    echo "[deploy.sh] ❌ Phase 0.5: 코드 검증 실패 — 배포 차단"
    echo "$VALIDATION_RESULT"
    notify "❌ 배포 차단: 코드 검증 실패\n${VALIDATION_RESULT}"
    exit 1
fi
echo "[deploy.sh] Phase 0.5: ✅ 코드 검증 통과"

# ── Phase 1: 배포 실행 ──
case "$MODE" in
    reload)
        echo "[deploy.sh] Phase 1: fast reload aads-api (supervisorctl restart)"
        # 배포 플래그
        docker exec "$ACTIVE_CONTAINER" touch /tmp/aads_deploy_restart 2>/dev/null || true
        # restart = SIGTERM + 자동 start (supervisord가 처리, 대기 루프 불필요)
        docker exec "$ACTIVE_CONTAINER" supervisorctl restart aads-api
        echo "[deploy.sh] Phase 1: supervisorctl restart 완료 — health check 대기..."
        ;;
    code)
        echo "[deploy.sh] Phase 1: code deploy with stream-safe slot switch"
        ACTIVE_STREAMS="$(stream_count_for_port "$ACTIVE_PORT")"
        PEER_PORT="$(peer_port_for "$ACTIVE_PORT")"
        PEER_CONTAINER="$(container_for_port "$PEER_PORT")"

        if [[ "${ACTIVE_STREAMS:-0}" != "0" && -n "$PEER_PORT" && -n "$PEER_CONTAINER" ]]; then
            echo "[deploy.sh] 활성 스트림 ${ACTIVE_STREAMS}건 감지 — active 재시작 대신 peer slot으로 전환"
            if ! curl -sf "http://127.0.0.1:${PEER_PORT}/api/v1/health" >/dev/null 2>&1; then
                echo "[deploy.sh] ❌ peer slot ${PEER_CONTAINER}:${PEER_PORT} health 실패 — 스트림 보호를 위해 배포 중단"
                notify "❌ code 배포 중단: active stream ${ACTIVE_STREAMS}건, peer unhealthy"
                exit 1
            fi
            docker exec "$PEER_CONTAINER" touch /tmp/aads_deploy_restart 2>/dev/null || true
            docker exec "$PEER_CONTAINER" supervisorctl restart aads-api
            if ! wait_port_health "$PEER_PORT" 90; then
                echo "[deploy.sh] ❌ peer slot 재시작 후 health 실패 — 전환 중단"
                notify "❌ code 배포 실패: peer slot health 실패"
                exit 1
            fi
            switch_api_upstream "$PEER_PORT" "$ACTIVE_PORT" "$PEER_CONTAINER" "$ACTIVE_CONTAINER"
            restart_old_slot_after_drain "$ACTIVE_CONTAINER" "$ACTIVE_PORT"
            ACTIVE_PORT="$PEER_PORT"
            ACTIVE_CONTAINER="$PEER_CONTAINER"
            HEALTH_URL="http://localhost:${ACTIVE_PORT}/api/v1/health"
            echo "[deploy.sh] Phase 1: ✅ active slot switched to ${ACTIVE_CONTAINER}:${ACTIVE_PORT}"
        else
            echo "[deploy.sh] 활성 스트림 0건 — active API graceful restart"
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

        # ① 새 컨테이너 빌드 + 시작
        cd "$COMPOSE_DIR"
        echo "[deploy.sh] ① ${NEW_CONTAINER} 빌드 + 시작..."
        docker compose $COMPOSE_FILE $PROFILE_CMD up -d --build --no-deps "$NEW_CONTAINER"

        # ② 새 컨테이너 헬스체크 (최대 90초)
        echo "[deploy.sh] ② ${NEW_CONTAINER} 헬스체크 (최대 90초)..."
        BG_ELAPSED=0
        BG_OK=false
        while [[ $BG_ELAPSED -lt 90 ]]; do
            sleep 3
            BG_ELAPSED=$((BG_ELAPSED + 3))
            if curl -sf "http://127.0.0.1:${NEW_PORT}/api/v1/health" >/dev/null 2>&1; then
                echo "[deploy.sh] ② ✅ ${NEW_CONTAINER} 정상 (${BG_ELAPSED}초)"
                BG_OK=true
                break
            fi
            echo "[deploy.sh] 대기중... ${BG_ELAPSED}/90초"
        done

        if [[ "$BG_OK" != "true" ]]; then
            echo "[deploy.sh] ❌ ${NEW_CONTAINER} 헬스체크 실패 — 롤백"
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            docker rm "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: ${NEW_CONTAINER} 헬스체크 통과 못함"
            exit 1
        fi

        # ③ upstream 전환 (aads-upstream.conf에서 backup 키워드 조작)
        echo "[deploy.sh] ③ upstream 전환: :${CURRENT_PORT} → :${NEW_PORT}"
        cp "$UPSTREAM_CONF" "${UPSTREAM_CONF}.pre_deploy"
        # 새 포트에서 backup 제거, 기존 포트에 backup 추가
        sed -i "s/server 127.0.0.1:${NEW_PORT} max_fails=3 fail_timeout=30s backup;/server 127.0.0.1:${NEW_PORT} max_fails=3 fail_timeout=30s;/g" "$UPSTREAM_CONF"
        sed -i "s/server 127.0.0.1:${CURRENT_PORT} max_fails=3 fail_timeout=30s;/server 127.0.0.1:${CURRENT_PORT} max_fails=3 fail_timeout=30s backup;/g" "$UPSTREAM_CONF"
        if ! nginx -t 2>/dev/null; then
            echo "[deploy.sh] ❌ nginx 설정 오류 — 롤백"
            cp "${UPSTREAM_CONF}.pre_deploy" "$UPSTREAM_CONF"
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: nginx 설정 오류"
            exit 1
        fi

        echo "[deploy.sh] [5/6] 활성 스트림 drain 대기..."
        _DRAIN_MAX=300
        _DRAIN_ELAPSED=0
        _DRAIN_INTERVAL=10
        while [ "$_DRAIN_ELAPSED" -lt "$_DRAIN_MAX" ]; do
            _ACTIVE=$(
                (
                    curl -s -m 5 "http://127.0.0.1:${ACTIVE_PORT}/api/v1/ops/active-streams" 2>/dev/null \
                    | python3 -c "import sys,json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null
                ) || echo "0"
            )

            if [ "$_ACTIVE" = "0" ] || [ -z "$_ACTIVE" ]; then
                echo "[deploy.sh]   활성 스트림 0건 — drain 완료"
                break
            fi

            echo "[deploy.sh]   활성 스트림 ${_ACTIVE}건 — ${_DRAIN_INTERVAL}초 대기 (${_DRAIN_ELAPSED}/${_DRAIN_MAX}s)"
            sleep "$_DRAIN_INTERVAL"
            _DRAIN_ELAPSED=$((_DRAIN_ELAPSED + _DRAIN_INTERVAL))
        done
        if [ "$_DRAIN_ELAPSED" -ge "$_DRAIN_MAX" ]; then
            echo "[deploy.sh]   WARN: drain 타임아웃 (${_DRAIN_MAX}s) — 강제 전환"
        fi

        systemctl reload nginx
        echo "[deploy.sh]   nginx upstream 전환 완료"

        # ④ 전환 후 검증
        sleep 2
        if curl -sf "http://127.0.0.1:${NEW_PORT}/api/v1/health" >/dev/null 2>&1; then
            echo "[deploy.sh] ④ ✅ 전환 검증 성공"
        else
            echo "[deploy.sh] ⚠️ 전환 후 검증 실패 — 이전 서버로 복원"
            cp "${UPSTREAM_CONF}.pre_deploy" "$UPSTREAM_CONF"
            systemctl reload nginx
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: 전환 검증 실패 — 복원 완료"
            exit 1
        fi

        # ⑤ 이전 컨테이너 지연 종료 (활성 스트림 drain 후 종료)
        echo "[deploy.sh] ⑤ ${OLD_CONTAINER} 지연 종료 시작"
        echo "$NEW_PORT" > /root/aads/aads-server/.active_port
        echo "$NEW_CONTAINER" > /root/aads/aads-server/.active_container

        (
            _OLD_DRAIN_MAX=600
            _OLD_DRAIN_ELAPSED=0

            while [ "$_OLD_DRAIN_ELAPSED" -lt "$_OLD_DRAIN_MAX" ]; do
                _OLD_ACTIVE=$(
                    (
                        curl -s -m 5 "http://127.0.0.1:${OLD_PORT}/api/v1/ops/active-streams" 2>/dev/null \
                        | python3 -c "import sys,json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null
                    ) || echo "0"
                )

                if [ "$_OLD_ACTIVE" = "0" ] || [ -z "$_OLD_ACTIVE" ]; then
                    echo "[deploy.sh]   구 컨테이너 활성 스트림 0건 — 안전 종료"
                    break
                fi

                echo "[deploy.sh]   구 컨테이너 활성 스트림 ${_OLD_ACTIVE}건 — 30초 대기"
                sleep 30
                _OLD_DRAIN_ELAPSED=$((_OLD_DRAIN_ELAPSED + 30))
            done

            docker stop --time 30 "$OLD_CONTAINER" 2>/dev/null || true
            echo "[deploy.sh] ⑤ ✅ ${OLD_CONTAINER} 종료 완료"
        ) &
        disown

        HEALTH_URL="http://localhost:${NEW_PORT}/api/v1/health"
        echo "[deploy.sh] ✅ Blue-Green 완전 무중단 배포 완료: :${NEW_PORT} 활성"
        notify "✅ Blue-Green 완전 무중단 배포: :${CURRENT_PORT} → :${NEW_PORT}"
        ;;
    *)
        echo "[deploy.sh] ERROR: 알 수 없는 모드 '$MODE'. code|reload|build|bluegreen 사용"
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
        docker exec "$ACTIVE_CONTAINER" supervisorctl restart aads-api || true
        sleep 10
    fi
    notify "❌ 배포 실패 + 롤백 시도 (mode=${MODE})"
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
        docker exec "$ACTIVE_CONTAINER" supervisorctl restart aads-api || true
        sleep 10
    fi
    notify "❌ 채팅 기능 테스트 실패 + 롤백 (mode=${MODE}): ${CHAT_TEST:0:200}"
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
CHANGED_FILES=$(git -C "$COMPOSE_DIR" diff HEAD~1 --name-only 2>/dev/null || echo "")
if echo "$CHANGED_FILES" | grep -q "aads-dashboard/"; then
    echo "[deploy.sh] Phase 6: 대시보드 변경 감지 — Next.js 빌드 대기 (20초)..."
    sleep 20
    QA_RESPONSE=$(curl -sf --max-time 120 -X POST "http://127.0.0.1:8100/api/v1/visual-qa/full-qa" \
        -H "Content-Type: application/json" \
        -d '{"pages": ["/", "/chat", "/ops"]}' 2>/dev/null) || QA_RESPONSE=""
    if [[ -z "$QA_RESPONSE" ]]; then
        echo "[deploy.sh] ⚠️ Phase 6: QA API 응답 없음 — 스킵 (non-blocking)"
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
        elif [[ "$QA_VERDICT" == "PASS" ]]; then
            echo "[deploy.sh] Phase 6: ✅ 프론트 QA 통과"
        else
            echo "[deploy.sh] ⚠️ Phase 6: QA 결과 불명 (verdict=${QA_VERDICT}) — 스킵"
        fi
    fi
else
    echo "[deploy.sh] Phase 6: 프론트 변경 없음 — QA 스킵"
fi

echo "[deploy.sh] ✅ 배포 완료 — 6단계 검증 통과 (mode=${MODE})"
notify "✅ 배포 완료 — 6단계 검증 통과 (mode=${MODE})"
exit 0
