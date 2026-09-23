#!/bin/bash
# AADS standby 슬롯 동기화 — 트래픽 전환도 재빌드도 하지 않고, 대기 슬롯만
# 활성 슬롯이 실제로 돌리는 이미지로 맞춘다.
#
# 왜 필요한가.
#   deploy.sh bluegreen 의 마지막 단계인 standby 동기화는 대기 슬롯에 살아 있는
#   채팅 턴이 남아 있으면 drain timeout 으로 blocked 된다. 이 동작 자체는 옳다 —
#   진행 중인 CEO 턴을 끊지 않는 것이 우선이다. 문제는 그 다음이다. blocked 된
#   뒤 슬롯을 수렴시킬 수단이 "전체 bluegreen 을 한 번 더 돌린다" 밖에 없었다.
#   그래서 nginx 가 backup 으로 물고 있는 대기 슬롯이 구 이미지로 남고, 활성
#   슬롯이 죽으면 조용히 구버전으로 폴백한다. 2026-09-14 run 418 이 그 사례로,
#   활성 :8102 는 44df37e3 인데 backup :8100 은 6시간 전 593360b1 로 남았다.
#   게다가 그 blocked 를 유발한 턴이 '지금 이 배포를 지시한 채팅 턴' 자신이면
#   재배포로는 영원히 풀리지 않는다(자기 차단 교착).
#
#   이 스크립트는 대기 슬롯이 비는 순간 그 슬롯만 활성 이미지로 재생성한다.
#   빌드 없음, nginx 전환 없음, 활성 슬롯 무손상.
#
# 사용법:
#   bash scripts/sync-standby.sh            # 동기화 실행
#   bash scripts/sync-standby.sh --dry-run  # 판정만 하고 아무것도 바꾸지 않음
#
# 종료코드: 0=동기화 완료 또는 이미 동일 / 2=사전조건 위반 / 3=대기 슬롯 사용 중
#           (3 은 실패가 아니라 "나중에 다시" 라는 뜻이다)

set -Eeuo pipefail

# 2026-09-14 실측: SSH/도구 래퍼가 55s 에서 끊기면서 재생성 직후 검증 단계가
# 통째로 날아갔다(컨테이너는 떴는데 digest 확인·resume 플래그 처리가 누락).
# 부모가 죽어도 검증까지는 끝내야 한다.
trap '' HUP
trap '' SIGPIPE 2>/dev/null || true

STATE_DIR="${AADS_DEPLOY_STATE_DIR:-/root/aads/aads-server}"
COMPOSE_DIR="${AADS_DEPLOY_SOURCE_DIR:-$STATE_DIR}"
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.prod.yml"
ENV_FILE="${AADS_RUNTIME_ENV_FILE:-${STATE_DIR}/.env}"
LOG_FILE="${STATE_DIR}/logs/standby-sync.log"
AUDIT_LOG="${AADS_CONTROL_AUDIT_LOG:-/var/log/aads-control-audit.jsonl}"
HEALTH_WAIT="${AADS_STANDBY_SYNC_HEALTH_WAIT:-90}"

BLUE_CONTAINER="aads-server"
BLUE_PORT=8100
GREEN_CONTAINER="aads-server-green"
GREEN_PORT=8102

DRY_RUN=false
DEPLOY_RUN_ID=""

mkdir -p "$(dirname "$LOG_FILE")"

log() { printf '[sync-standby] %s %s\n' "$(date '+%F %T %Z')" "$*" | tee -a "$LOG_FILE"; }

audit() {
    local result="$1" detail="$2"
    printf '{"ts":"%s","action":"sync-standby","target":"%s","result":"%s","detail":"%s"}\n' \
        "$(date -Is)" "${standby_container:-unknown}:${standby_port:-unknown}" "$result" "$detail" \
        >> "$AUDIT_LOG" 2>/dev/null || true
}

die() { log "ERROR: $*"; audit "failed" "$*"; exit 2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --deploy-run-id)
            DEPLOY_RUN_ID="${2:-}"
            shift
            ;;
        *) die "알 수 없는 인자: $1" ;;
    esac
    shift
done
[[ -z "$DEPLOY_RUN_ID" || "$DEPLOY_RUN_ID" =~ ^[0-9]+$ ]] \
    || die "deploy run id는 숫자여야 한다: ${DEPLOY_RUN_ID}"

# ── 1. 활성 슬롯 확인 ────────────────────────────────────────────────────────
active_container="$(tr -d '[:space:]' < "${STATE_DIR}/.active_container" 2>/dev/null || true)"
active_port="$(tr -d '[:space:]' < "${STATE_DIR}/.active_port" 2>/dev/null || true)"
[[ -n "$active_container" && -n "$active_port" ]] || die "활성 슬롯 상태 파일을 읽을 수 없다"

case "${active_container}:${active_port}" in
    "${GREEN_CONTAINER}:${GREEN_PORT}")
        standby_container="$BLUE_CONTAINER"; standby_port="$BLUE_PORT"; profile_args=() ;;
    "${BLUE_CONTAINER}:${BLUE_PORT}")
        standby_container="$GREEN_CONTAINER"; standby_port="$GREEN_PORT"; profile_args=(--profile green) ;;
    *)
        die "알 수 없는 활성 슬롯 조합: ${active_container}:${active_port}" ;;
esac
log "활성=${active_container}:${active_port} / 대기=${standby_container}:${standby_port}"

# ── 2. 배포와의 경합 차단 ────────────────────────────────────────────────────
# deploy.sh 가 잡는 락을 먼저 시도한다. 잡히면 진행 중인 배포가 없다는 뜻이고,
# 스크립트가 끝날 때까지 새 배포도 들어오지 못한다.
exec 7>"/tmp/aads-deploy.flock"
flock -n 7 || die "배포가 진행 중이다 (aads-deploy.flock). 배포 종료 후 다시 실행하라"
exec 9>"/tmp/aads-standby-sync.lock"
flock -w 30 9 || die "다른 standby 동기화가 진행 중이다"

# ── 3. 이미지 비교 ───────────────────────────────────────────────────────────
active_tag="$(docker inspect "$active_container" --format '{{.Config.Image}}' 2>/dev/null || true)"
active_digest="$(docker inspect "$active_container" --format '{{.Image}}' 2>/dev/null || true)"
standby_digest="$(docker inspect "$standby_container" --format '{{.Image}}' 2>/dev/null || true)"
[[ -n "$active_tag" && -n "$active_digest" ]] || die "활성 컨테이너 이미지를 읽을 수 없다"
release_sha="${active_tag#aads-server:}"
[[ -n "$release_sha" && "$release_sha" != "$active_tag" ]] || die "활성 이미지 태그 형식이 예상과 다르다: ${active_tag}"

certify_deferred_run() {
    [[ -n "$DEPLOY_RUN_ID" ]] || return 0
    local release_sql digest_sql
    release_sql="${release_sha//\'/\'\'}"
    digest_sql="${active_digest//\'/\'\'}"
    docker exec aads-postgres psql -U aads -d aads -v ON_ERROR_STOP=1 -qAtc "
        WITH updated AS (
            UPDATE deploy_runs
            SET status='success', phase='completed', phase_completed_at=NOW(),
                updated_at=NOW(), last_heartbeat_at=NOW(),
                image_digest='${digest_sql}', standby_digest='${digest_sql}',
                error_summary=NULL
            WHERE id=${DEPLOY_RUN_ID}
              AND project='AADS'
              AND release_sha='${release_sql}'
              AND status='success_partial'
            RETURNING id
        )
        INSERT INTO deploy_phase_events(
            deploy_run_id, phase, status, phase_started_at, phase_completed_at,
            duration_ms, current_slot, candidate_slot, image_digest,
            standby_digest, error_summary, metadata
        )
        SELECT id, 'standby_same_digest_sync_retry', 'success', NOW(), NOW(),
               0, '${active_port}', '${standby_port}', '${digest_sql}',
               '${digest_sql}', NULL, jsonb_build_object('source','sync-standby.sh')
        FROM updated;
    " >/dev/null
    log "deploy_runs#${DEPLOY_RUN_ID} success_partial → success 인증 반영"
}

if [[ "$active_digest" == "$standby_digest" ]]; then
    log "이미 동일한 이미지다 (${active_tag}). 할 일 없음."
    # --dry-run must not certify a deploy even when slots have converged;
    # certification is a database write, unlike the diagnostic log.
    if [[ "$DRY_RUN" != "true" ]]; then
        certify_deferred_run
    fi
    audit "skipped" "already same digest"
    exit 0
fi
log "드리프트 감지: 활성=${active_digest:7:12} / 대기=${standby_digest:7:12}"

docker image inspect "$active_tag" >/dev/null 2>&1 || die "이미지 ${active_tag} 가 로컬에 없다"

# ── 4. 사전조건: 활성 슬롯 정상 + 대기 슬롯 비어 있음 ────────────────────────
curl -sf --max-time 5 "http://127.0.0.1:${active_port}/api/v1/health" >/dev/null \
    || die "활성 슬롯 :${active_port} 가 비정상이다. 대기 슬롯을 건드리지 않는다"

streams="$(curl -sf --max-time 5 "http://127.0.0.1:${standby_port}/api/v1/ops/active-streams" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("executing_count","unknown"))' 2>/dev/null || echo unknown)"
if [[ "$streams" != "0" ]]; then
    log "대기 슬롯 :${standby_port} 에 실행 중 턴이 있다 (executing_count=${streams}). 중단한다 — 진행 중 턴을 끊지 않는다."
    audit "deferred" "standby busy executing_count=${streams}"
    exit 3
fi
log "사전조건 통과: 활성 healthy, 대기 executing_count=0"

if [[ "$DRY_RUN" == "true" ]]; then
    log "--dry-run: 여기서 멈춘다. 실행하면 ${standby_container} 를 ${active_tag} 로 재생성한다."
    audit "dry_run" "would recreate with ${active_tag}"
    exit 0
fi

# ── 5. 대기 슬롯 재생성 (빌드 없음, --no-deps 로 단일 서비스만) ──────────────
log "재생성: ${standby_container} → ${active_tag}"
cd "$COMPOSE_DIR"
AADS_RELEASE_SHA="$release_sha" COMPOSE_PROJECT_NAME="${AADS_COMPOSE_PROJECT_NAME:-aads-server}" \
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "${profile_args[@]}" \
    up -d --no-build --no-deps --force-recreate "$standby_container"

# ── 6. 검증 ─────────────────────────────────────────────────────────────────
waited=0
until curl -sf --max-time 3 "http://127.0.0.1:${standby_port}/api/v1/health" >/dev/null 2>&1; do
    [[ $waited -ge $HEALTH_WAIT ]] && die "대기 슬롯 :${standby_port} 헬스체크가 ${HEALTH_WAIT}s 안에 통과하지 못했다"
    sleep 3
    waited=$((waited + 3))
done
log "대기 슬롯 healthy (${waited}s)"

new_digest="$(docker inspect "$standby_container" --format '{{.Image}}' 2>/dev/null || true)"
[[ "$new_digest" == "$active_digest" ]] \
    || die "재생성 후에도 digest 불일치: 활성=${active_digest} 대기=${new_digest}"

# 대기 슬롯이 중단된 실행을 자기가 이어받지 않도록 소유권 플래그를 내린다.
# (deploy.sh 의 standby 동기화와 동일한 처리)
docker exec "$standby_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true

# 활성 슬롯이 그대로인지 최종 확인 — 이 스크립트는 절대 트래픽을 옮기지 않는다.
now_active="$(tr -d '[:space:]' < "${STATE_DIR}/.active_container" 2>/dev/null || true)"
now_port="$(tr -d '[:space:]' < "${STATE_DIR}/.active_port" 2>/dev/null || true)"
[[ "$now_active" == "$active_container" && "$now_port" == "$active_port" ]] \
    || die "활성 슬롯이 바뀌었다: ${now_active}:${now_port}"
curl -sf --max-time 5 "http://127.0.0.1:${active_port}/api/v1/health" >/dev/null \
    || die "활성 슬롯이 동기화 후 비정상이다"

log "✅ 동기화 완료: 양 슬롯 ${active_tag} (${active_digest:7:12}), 활성 ${active_container}:${active_port} 무변경"
certify_deferred_run
audit "success" "standby synced to ${active_tag}"
exit 0
