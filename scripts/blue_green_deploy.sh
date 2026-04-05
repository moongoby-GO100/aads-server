#!/bin/bash
# AADS Blue-Green 무중단 배포 스크립트
# [2026-04-02] 신규 작성 — CEO 지시: 무중단 배포 체계 구축
# [2026-04-02] P2: 배포 이력 DB 자동 기록 추가
#
# 사용법: ./scripts/blue_green_deploy.sh [--build]
#   --build: Dockerfile/dependencies 변경 시 이미지 리빌드
#   (없으면 코드 변경만 — Hot-Reload 무중단 처리)
#
# 플로우:
#   코드만 변경 → Hot-Reload SIGHUP (다운타임 0초 무중단)
#   컨테이너 리빌드 → green 시작 → HC 통과 → nginx 전환 → blue 정지

set -euo pipefail

COMPOSE_DIR="/root/aads/aads-server"
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.prod.yml"
HEALTH_BLUE="http://127.0.0.1:8100/api/v1/health"
HEALTH_GREEN="http://127.0.0.1:8102/api/v1/health"
HEALTH_EXTERNAL="https://aads.newtalk.kr/api/v1/health"
NGINX_UPSTREAM="/etc/nginx/conf.d/aads-upstream.conf"
NGINX_CONF="/etc/nginx/conf.d/aads.conf"
LOG="/var/log/blue_green_deploy.log"
BUILD_FLAG="${1:-}"
DEPLOY_START=$(date +%s)
DEPLOY_ID=""

# .env에서 텔레그램 변수 로드
TELEGRAM_BOT_TOKEN=$(grep -oP '^TELEGRAM_BOT_TOKEN=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
TELEGRAM_CHAT_ID=$(grep -oP '^TELEGRAM_CHAT_ID=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || true)
# .env에서 DB 접속 정보 로드
DB_HOST=$(grep -oP '^DB_HOST=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || echo "aads-postgres")
DB_PORT=$(grep -oP '^DB_PORT=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || echo "5432")
DB_NAME=$(grep -oP '^DB_NAME=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || echo "aads")
DB_USER=$(grep -oP '^DB_USER=\K.*' "${COMPOSE_DIR}/.env" 2>/dev/null || echo "aads")

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S KST')] $1" | tee -a "$LOG"; }
notify() {
    log "$1"
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        curl -sf -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_CHAT_ID}" \
            -d text="🚀 [AADS Deploy] $1" >/dev/null 2>&1 || true
    fi
}

# ── DB 기록 함수 ──────────────────────────────────
db_record_start() {
    local deploy_type="$1" trigger_by="${2:-script}"
    local git_commit git_message
    git_commit=$(cd "$COMPOSE_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
    git_message=$(cd "$COMPOSE_DIR" && git log -1 --format='%s' 2>/dev/null | head -c 200 || echo "")
    # 특수문자 이스케이프
    git_message=$(echo "$git_message" | sed "s/'/''/g")
    DEPLOY_ID=$(docker exec aads-server python3 -c "
import asyncio, asyncpg, os
async def run():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    row = await conn.fetchrow('''
        INSERT INTO deploy_history (deploy_type, project, trigger_by, git_commit, git_message, status)
        VALUES (\$1, 'AADS', \$2, \$3, \$4, 'started') RETURNING id
    ''', '${deploy_type}', '${trigger_by}', '${git_commit}', '${git_message}')
    print(row['id'])
    await conn.close()
asyncio.run(run())
" 2>/dev/null || echo "")
    if [[ -n "$DEPLOY_ID" ]]; then
        log "📝 배포 이력 기록 시작 (id=$DEPLOY_ID, type=$deploy_type)"
    fi
}

db_record_finish() {
    local status="$1" error_msg="${2:-}"
    if [[ -z "$DEPLOY_ID" ]]; then return; fi
    local duration=$(( $(date +%s) - DEPLOY_START ))
    error_msg=$(echo "$error_msg" | sed "s/'/''/g" | head -c 500)
    docker exec aads-server python3 -c "
import asyncio, asyncpg, os
async def run():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    await conn.execute('''
        UPDATE deploy_history SET status=\$1, duration_s=\$2, error_msg=\$3, finished_at=NOW()
        WHERE id=\$4
    ''', '${status}', ${duration}, '${error_msg}', ${DEPLOY_ID})
    await conn.close()
asyncio.run(run())
" 2>/dev/null || log "⚠️ DB 기록 실패 (무시)"
    log "📝 배포 이력 기록 완료 (id=$DEPLOY_ID, status=$status, ${duration}초)"
}

wait_health() {
    local url="$1" max_wait="$2" label="$3"
    local elapsed=0
    while [ "$elapsed" -lt "$max_wait" ]; do
        if curl -sf --max-time 5 "$url" >/dev/null 2>&1; then
            log "✅ ${label} health OK (${elapsed}초)"
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    log "❌ ${label} health 실패 (${max_wait}초 초과)"
    return 1
}

# ──────────────────────────────────────────────
# Case 1: 코드만 변경 (볼륨마운트 반영 + 프로세스 재시작)
# ──────────────────────────────────────────────
if [ "$BUILD_FLAG" != "--build" ]; then
    db_record_start "code_only" "${DEPLOY_TRIGGER:-script}"
    notify "📦 코드 변경 배포 시작 (Hot-Reload — 0초 무중단)"

    # 문법 검증 (py_compile)
    SYNTAX_ERR=$(docker exec aads-server python3 -c "
import py_compile, os, sys
errors = []
for root, dirs, files in os.walk('/app/app'):
    for f in files:
        if f.endswith('.py'):
            try:
                py_compile.compile(os.path.join(root, f), doraise=True)
            except py_compile.PyCompileError as e:
                errors.append(str(e))
if errors:
    print('SYNTAX_ERROR: ' + '; '.join(errors[:3]))
    sys.exit(1)
else:
    print('SYNTAX_OK')
" 2>&1)

    if echo "$SYNTAX_ERR" | grep -q "SYNTAX_ERROR"; then
        notify "❌ 문법 오류 감지 — 배포 중단: ${SYNTAX_ERR}"
        db_record_finish "failed" "$SYNTAX_ERR"
        exit 1
    fi

    bash /root/aads/aads-server/scripts/reload-api.sh
    sleep 2

    if wait_health "$HEALTH_BLUE" 30 "blue(restart)"; then
        notify "✅ 코드 배포 완료 — 정상 가동"
        db_record_finish "success"
    else
        notify "❌ restart 후 health 실패 — CEO 확인 필요"
        db_record_finish "failed" "health check failed after restart"
        exit 1
    fi
    exit 0
fi

# ──────────────────────────────────────────────
# Case 2: 컨테이너 리빌드 (Blue-Green 전환)
# ──────────────────────────────────────────────
db_record_start "blue_green" "${DEPLOY_TRIGGER:-script}"
notify "🔄 Blue-Green 배포 시작 (--build)"

# Step 1: 현재 blue 정상 확인
if ! wait_health "$HEALTH_BLUE" 10 "blue(사전검증)"; then
    notify "⚠️ 현재 blue가 이미 unhealthy — 단순 리빌드로 전환"
    cd "$COMPOSE_DIR"
    docker compose -f "$COMPOSE_FILE" up -d --build --no-deps aads-server
    if wait_health "$HEALTH_BLUE" 90 "blue(리빌드)"; then
        notify "✅ 단순 리빌드 완료"
        db_record_finish "success"
    else
        db_record_finish "failed" "rebuild health check failed"
    fi
    exit 0
fi

# Step 2: green 이미지 빌드 + 시작
log "Step 2: green 컨테이너 빌드 및 시작..."
cd "$COMPOSE_DIR"
docker compose -f "$COMPOSE_FILE" --profile green up -d --build aads-server-green

# Step 3: green health 대기 (최대 90초)
if ! wait_health "$HEALTH_GREEN" 90 "green"; then
    notify "❌ green health 실패 — 롤백 (green 제거)"
    docker compose -f "$COMPOSE_FILE" --profile green stop aads-server-green
    docker compose -f "$COMPOSE_FILE" --profile green rm -f aads-server-green
    db_record_finish "failed" "green health check failed"
    exit 1
fi

# Step 4: nginx upstream을 green(8102)으로 전환
log "Step 4: nginx → green(8102) 전환..."
cp "$NGINX_CONF" "${NGINX_CONF}.pre_deploy"
sed -i 's|http://127.0.0.1:8100/api/v1/|http://127.0.0.1:8102/api/v1/|g' "$NGINX_CONF"
sed -i 's|http://127.0.0.1:8100/api/v1/pc-agent/ws/|http://127.0.0.1:8102/api/v1/pc-agent/ws/|g' "$NGINX_CONF"

# nginx 설정 검증
if ! nginx -t 2>/dev/null; then
    notify "❌ nginx 설정 오류 — 롤백"
    cp "${NGINX_CONF}.pre_deploy" "$NGINX_CONF"
    docker compose -f "$COMPOSE_FILE" --profile green stop aads-server-green
    db_record_finish "rolled_back" "nginx config error"
    exit 1
fi

systemctl reload nginx
sleep 2

# Step 5: 외부 health 확인
if ! wait_health "$HEALTH_EXTERNAL" 15 "external(green 경유)"; then
    notify "❌ 외부 접근 실패 — nginx 롤백"
    cp "${NGINX_CONF}.pre_deploy" "$NGINX_CONF"
    nginx -t && systemctl reload nginx
    db_record_finish "rolled_back" "external health failed after nginx switch"
    exit 1
fi

# Step 6: blue 정지
log "Step 6: blue 컨테이너 정지..."
docker compose -f "$COMPOSE_FILE" stop aads-server

# Step 7: 완료
notify "✅ Blue-Green 배포 완료 — green(8102) 서비스 중"
log "⚠️ 현재 nginx가 8102를 가리킴. 다음 배포 시 역전환 또는 blue 재시작 필요."
log "   복원: cp ${NGINX_CONF}.pre_deploy ${NGINX_CONF} && nginx -t && systemctl reload nginx"
db_record_finish "success"
