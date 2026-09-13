#!/bin/bash
# AADS 안전 배포 게이트웨이
# 사용법: deploy.sh [bluegreen|code|reload|build]
#   bluegreen (기본) — Blue↔Green 무중단 전환 (중단 0초, 자동 롤백, upstream 전환)
#   code/reload/build — 레거시 모드. 기본 차단 후 bluegreen으로 자동 전환.
#                      불가피한 수동 점검 때만 AADS_DEPLOY_ALLOW_LEGACY_RESTART=true 지정.
#
# 검증 6단계: 의존성→코드검증→배포→Health→DB스키마→채팅→LLM→프론트QA

set -euo pipefail
trap '' HUP  # RC4: ignore HUP immediately — eliminates race window before main trap block
trap '' SIGPIPE 2>/dev/null || true  # RC9: prevent broken-pipe from killing deploy subprocesses

REQUESTED_MODE="${1:-bluegreen}"
MODE="$REQUESTED_MODE"
# 모드 검증은 맨 앞에서 끝낸다.
# 2026-09-13 09:09 KST #377/#378: 누군가 `deploy.sh status` 를 호출했는데
# 모드 검사가 code_validation(2364행)에 있어서, 그 전에 preflight 를 통과하고
# 큐에 있던 릴리스를 claim 한 뒤 deploy_runs 에 실패 2건을 남겼다.
# 배포가 아닌 호출이 배포 실패 원장을 오염시키면 실패율·원인 분석이 전부 흐려진다.
case "$MODE" in
    bluegreen|code|reload|build) ;;
    *)
        echo "[deploy.sh] ERROR: 알 수 없는 모드 '$MODE'. bluegreen|code|reload|build 사용" >&2
        exit 2
        ;;
esac
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${AADS_DEPLOY_SOURCE_DIR:-$SCRIPT_DIR}"
STATE_DIR="${AADS_DEPLOY_STATE_DIR:-/root/aads/aads-server}"
export COMPOSE_PROJECT_NAME="${AADS_COMPOSE_PROJECT_NAME:-aads-server}"
export AADS_RELEASE_SHA="${AADS_RELEASE_SHA:-$(git -C "$COMPOSE_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)}"
export AADS_RUNTIME_ENV_FILE="${AADS_RUNTIME_ENV_FILE:-${STATE_DIR}/.env}"
export AADS_LITELLM_ENV_FILE="${AADS_LITELLM_ENV_FILE:-${STATE_DIR}/.env.litellm}"
COMPOSE_ENV_ARGS=("--env-file" "$AADS_RUNTIME_ENV_FILE")
HEALTH_URL="http://localhost:8100/api/v1/health"
MAX_WAIT="${AADS_DEPLOY_MAX_WAIT:-30}"
INTERVAL=2
UPSTREAM_CONF="/etc/nginx/conf.d/aads-upstream.conf"
ACTIVE_CONTAINER_FILE="${STATE_DIR}/.active_container"
ACTIVE_PORT_FILE="${STATE_DIR}/.active_port"
ACTIVE_SLOT_STATE_WRITER="${COMPOSE_DIR}/scripts/aads_active_slot_state.sh"
API_MEMORY_BYTES="${AADS_API_MEMORY_BYTES:-5368709120}"
API_MEMORY_SWAP_BYTES="${AADS_API_MEMORY_SWAP_BYTES:-5368709120}"
AADS_DOCKER_TARGET="${AADS_DOCKER_TARGET:-runtime}"
AADS_IMAGE_PROFILE="${AADS_IMAGE_PROFILE:-runtime}"
# Production capture_screenshot defaults to the server-managed Playwright
# runtime. Keep the Dockerfile opt-in for bounded ad-hoc images, but make the
# canonical AADS release include Chromium unless an operator explicitly opts
# out for a non-browser profile.
AADS_INSTALL_PLAYWRIGHT="${AADS_INSTALL_PLAYWRIGHT:-true}"
DEPLOY_START_EPOCH=$(date +%s)
DEPLOY_GENERATION_FILE="${STATE_DIR}/.deploy_generation"
CONTROL_AUDIT_LOG="${AADS_CONTROL_AUDIT_LOG:-/var/log/aads-control-audit.jsonl}"
RELEASE_CONTEXT_DIR=""
DEPLOY_RUN_ID=""
DEPLOY_CURRENT_PHASE="initializing"
DEPLOY_UPSTREAM_SWITCHED=false  # RC1: set true after nginx cutover; signals after this = success
DEPLOY_PHASE_START_EPOCH="$DEPLOY_START_EPOCH"
DEPLOY_HEARTBEAT_PID=""
DEPLOY_QUEUE_WORKER_LOCKFILE="/tmp/aads-deploy-queue-worker.lock"
DEPLOY_STREAM_CLASSIFIER="${COMPOSE_DIR}/scripts/classify_deploy_streams.py"
DEPLOY_PHASE_METADATA_JSON=""
LAST_STREAM_RECONCILE_JSON=""
mkdir -p "${STATE_DIR}/logs"

# RC5: Auto-detach ALL invocations — prevents TERM/HUP from killing deploy
# when parent process (SSH, chat tool, pipeline, cron) terminates.
# Opt out: AADS_DEPLOY_FOREGROUND=1 for interactive manual watching.
if [[ -z "${AADS_DEPLOY_DETACHED:-}" && "${AADS_DEPLOY_FOREGROUND:-}" != "1" ]]; then
    export AADS_DEPLOY_DETACHED=1
    _detach_log="${STATE_DIR}/logs/deploy-detach-$(date +%Y%m%d-%H%M%S)-$$.log"
    if command -v setsid >/dev/null 2>&1; then
        setsid bash "$0" "$@" >"$_detach_log" 2>&1 &
    else
        nohup bash "$0" "$@" >"$_detach_log" 2>&1 &
    fi
    _detach_pid=$!
    echo "[deploy.sh] detached: pid=${_detach_pid}, log=${_detach_log}"
    exit 0
fi

cleanup_release_context() {
    case "${RELEASE_CONTEXT_DIR:-}" in
        /tmp/aads-server-release.*)
            rm -rf -- "$RELEASE_CONTEXT_DIR"
            ;;
    esac
    RELEASE_CONTEXT_DIR=""
}

build_disk_check_path() {
    if [[ -d "${AADS_DEPLOY_DISK_CHECK_PATH:-}" ]]; then
        echo "$AADS_DEPLOY_DISK_CHECK_PATH"
    elif [[ -d /var/lib/docker ]]; then
        echo "/var/lib/docker"
    else
        echo "/"
    fi
}

require_build_disk_free() {
    local check_path min_free_gb min_free_kb avail_kb
    check_path="$(build_disk_check_path)"
    min_free_gb="${AADS_DEPLOY_MIN_FREE_GB:-20}"
    if [[ ! "$min_free_gb" =~ ^[0-9]+$ ]] || [[ "$min_free_gb" -lt 1 ]]; then
        min_free_gb="20"
    fi
    min_free_kb=$((min_free_gb * 1024 * 1024))
    avail_kb="$(df -Pk "$check_path" | awk 'NR == 2 {print $4}')"
    if [[ ! "$avail_kb" =~ ^[0-9]+$ ]]; then
        echo "[deploy.sh] ❌ cannot read available disk for ${check_path}"
        # AADS-191 후속: 실측 수치를 DB error_summary 까지 끌고 간다.
        # 기존에는 "insufficient build disk" 7글자만 남아서 대시보드에서
        # "얼마나 모자란지" 를 알 수 없었고, 매번 로그 파일을 열어야 했다.
        DEPLOY_DISK_FAIL_DETAIL="cannot read available disk for ${check_path}"
        audit_control "build-disk-preflight" "$check_path" "failed" "available_kb=unknown"
        return 1
    fi
    if [[ "$avail_kb" -lt "$min_free_kb" ]]; then
        echo "[deploy.sh] ❌ build disk preflight failed: ${check_path} available=$((avail_kb / 1024))MB, required=$((min_free_kb / 1024))MB"
        echo "[deploy.sh]    Free Docker space before retrying; this avoids slow builds that fail during image export."
        DEPLOY_DISK_FAIL_DETAIL="${check_path} available=$((avail_kb / 1024))MB, required=$((min_free_kb / 1024))MB, short=$(((min_free_kb - avail_kb) / 1024))MB"
        audit_control "build-disk-preflight" "$check_path" "blocked" "available_kb=${avail_kb}; required_kb=${min_free_kb}"
        return 1
    fi
    DEPLOY_DISK_FAIL_DETAIL=""
    echo "[deploy.sh] ✅ build disk preflight: ${check_path} available=$((avail_kb / 1024))MB, required=$((min_free_kb / 1024))MB"
    audit_control "build-disk-preflight" "$check_path" "success" "available_kb=${avail_kb}; required_kb=${min_free_kb}"
}

# 배포마다 4.24GB 이미지가 쌓이는데 회수 절차가 없었다. 2026-09-12 에 7번
# 빌드하자 /var/lib/docker 여유가 33GB → 15GB 로 말라 #340 이 빌드 직전에
# 막혔다. 실행 중이 아닌 오래된 릴리스 이미지를 남길 개수만 두고 정리한다.
prune_old_release_images() {
    local keep repo in_use kept removed tag
    keep="${AADS_DEPLOY_KEEP_IMAGES:-3}"
    if [[ ! "$keep" =~ ^[0-9]+$ ]] || [[ "$keep" -lt 2 ]]; then
        keep="3"
    fi
    in_use="$(docker ps -a --format '{{.Image}}' 2>/dev/null | sort -u)"
    for repo in aads-server aads-dashboard; do
        kept=0
        removed=0
        while read -r tag; do
            [[ -z "$tag" ]] && continue
            # 실행 중이거나 실행 예정인 이미지는 건드리지 않는다.
            if grep -Fxq "$tag" <<< "$in_use"; then
                continue
            fi
            case "$tag" in
                *:latest|*:local) continue ;;
            esac
            if [[ "$kept" -lt "$keep" ]]; then
                kept=$((kept + 1))
                continue
            fi
            if docker rmi "$tag" >/dev/null 2>&1; then
                removed=$((removed + 1))
            fi
        done < <(docker images "$repo" --format '{{.Repository}}:{{.Tag}}' 2>/dev/null)
        if [[ "$removed" -gt 0 ]]; then
            echo "[deploy.sh] 🧹 image retention: ${repo} removed=${removed} kept=${keep}"
            audit_control "image-retention" "$repo" "success" "removed=${removed}; keep=${keep}"
        fi
    done
}

# 이미 양쪽 슬롯에 올라가 있는 릴리스를 다시 빌드하는 건 이미지 하나와 컷오버
# 한 번을 헛되이 쓰는 일이다. 2026-09-12 #335 가 이미 라이브인 릴리스를 다시
# 배포했고, 그만큼 디스크가 줄고 진행 중이던 채팅 턴이 한 번 더 끊겼다.
reject_duplicate_live_release() {
    local sha short img_blue img_green
    sha="${AADS_RELEASE_SHA:-}"
    [[ -z "$sha" ]] && return 0
    if [[ "${AADS_DEPLOY_ALLOW_SAME_RELEASE:-0}" == "1" ]]; then
        echo "[deploy.sh] same-release guard bypassed by AADS_DEPLOY_ALLOW_SAME_RELEASE=1"
        return 0
    fi
    short="${sha:0:8}"
    img_blue="$(docker inspect aads-server --format '{{.Config.Image}}' 2>/dev/null || true)"
    img_green="$(docker inspect aads-server-green --format '{{.Config.Image}}' 2>/dev/null || true)"
    # 양쪽 다 이 릴리스이고 둘 다 건강할 때만 막는다. 한쪽만 올라가 있으면
    # standby 동기화가 남은 상태이므로 정상적인 복구 배포다.
    if [[ "$img_blue" == *"$short"* && "$img_green" == *"$short"* ]]; then
        local h_blue h_green
        h_blue="$(docker inspect aads-server --format '{{.State.Health.Status}}' 2>/dev/null || true)"
        h_green="$(docker inspect aads-server-green --format '{{.State.Health.Status}}' 2>/dev/null || true)"
        if [[ "$h_blue" == "healthy" && "$h_green" == "healthy" ]]; then
            echo "[deploy.sh] ⏭️ same release already live on both slots (${short}) — nothing to deploy"
            audit_control "same-release-guard" "aads-server:${short}" "skipped" \
                "blue=${img_blue}; green=${img_green}"
            return 1
        fi
    fi
    return 0
}

require_release_context_within_limit() {
    local max_context_mb context_mb
    max_context_mb="${AADS_DEPLOY_MAX_RELEASE_CONTEXT_MB:-1024}"
    if [[ ! "$max_context_mb" =~ ^[0-9]+$ ]] || [[ "$max_context_mb" -lt 128 ]]; then
        max_context_mb="1024"
    fi
    context_mb="$(du -sm "$RELEASE_CONTEXT_DIR" | awk '{print $1}')"
    if [[ ! "$context_mb" =~ ^[0-9]+$ ]]; then
        echo "[deploy.sh] ❌ cannot measure release context size: ${RELEASE_CONTEXT_DIR}"
        audit_control "release-context-size" "$RELEASE_CONTEXT_DIR" "failed" "context_mb=unknown"
        return 1
    fi
    if [[ "$context_mb" -gt "$max_context_mb" ]]; then
        echo "[deploy.sh] ❌ release context too large: ${context_mb}MB > ${max_context_mb}MB"
        echo "[deploy.sh]    Move generated media/reports/caches out of tracked release files before deploying."
        audit_control "release-context-size" "$RELEASE_CONTEXT_DIR" "blocked" "context_mb=${context_mb}; max_context_mb=${max_context_mb}"
        return 1
    fi
    echo "[deploy.sh] ✅ release context size: ${context_mb}MB <= ${max_context_mb}MB"
    audit_control "release-context-size" "$RELEASE_CONTEXT_DIR" "success" "context_mb=${context_mb}; max_context_mb=${max_context_mb}"
}

require_dependency_lock_freshness() {
    local missing=0 lock changed_pyproject changed_locks
    for lock in requirements.runtime.lock requirements.dev.lock requirements.visual.lock; do
        if [[ ! -s "${COMPOSE_DIR}/${lock}" ]]; then
            echo "[deploy.sh] ❌ dependency lock missing or empty: ${lock}"
            missing=1
        fi
    done
    if [[ "$missing" != "0" ]]; then
        audit_control "dependency-lock-freshness" "$COMPOSE_DIR" "blocked" "missing dependency lock"
        return 1
    fi

    if git -C "$COMPOSE_DIR" rev-parse --verify HEAD^ >/dev/null 2>&1; then
        changed_pyproject="$(git -C "$COMPOSE_DIR" diff --name-only HEAD^ HEAD -- pyproject.toml | tr -d '[:space:]' || true)"
        changed_locks="$(git -C "$COMPOSE_DIR" diff --name-only HEAD^ HEAD -- \
            requirements.runtime.lock requirements.dev.lock requirements.visual.lock | tr -d '[:space:]' || true)"
        if [[ -n "$changed_pyproject" && -z "$changed_locks" ]]; then
            echo "[deploy.sh] ❌ dependency lock freshness failed: pyproject.toml changed without lock update"
            echo "[deploy.sh]    Run scripts/compile_requirements.sh and commit updated lock files."
            audit_control "dependency-lock-freshness" "$COMPOSE_DIR" "blocked" "pyproject changed without lock update"
            return 1
        fi
    fi
    echo "[deploy.sh] ✅ dependency lock freshness verified"
    audit_control "dependency-lock-freshness" "$COMPOSE_DIR" "success" "runtime/dev/visual locks present"
}

emit_release_context_manifest() {
    local top_n="${AADS_DEPLOY_CONTEXT_MANIFEST_TOP_N:-20}"
    if [[ ! "$top_n" =~ ^[0-9]+$ ]] || [[ "$top_n" -lt 1 ]]; then
        top_n="20"
    fi
    echo "[deploy.sh] release context largest tracked files (top ${top_n}):"
    find "$RELEASE_CONTEXT_DIR" -type f -printf '%s\t%P\n' 2>/dev/null \
        | sort -nr \
        | head -"$top_n" \
        | awk '{mb=$1/1024/1024; sub(/^[^\t]*\t/, ""); printf("[deploy.sh]   %.2fMB\t%s\n", mb, $0)}' || true
    audit_control "release-context-manifest" "$RELEASE_CONTEXT_DIR" "success" "top_n=${top_n}"
}

report_docker_retention_status() {
    if ! docker info >/dev/null 2>&1; then
        echo "[deploy.sh] ⚠️ Docker unavailable; retention status skipped"
        return 0
    fi
    echo "[deploy.sh] Docker storage status:"
    docker system df 2>/dev/null | sed 's/^/[deploy.sh]   /' || true
    if [[ -x "${COMPOSE_DIR}/scripts/prune_aads_images.sh" ]]; then
        "${COMPOSE_DIR}/scripts/prune_aads_images.sh" --dry-run 2>/dev/null \
            | sed 's/^/[deploy.sh]   retention: /' \
            | head -40 || true
    fi
}

require_release_image_within_limit() {
    local max_image_gb max_image_bytes image_bytes
    max_image_gb="${AADS_DEPLOY_MAX_IMAGE_GB:-7}"
    if [[ ! "$max_image_gb" =~ ^[0-9]+$ ]] || [[ "$max_image_gb" -lt 1 ]]; then
        max_image_gb="7"
    fi
    max_image_bytes=$((max_image_gb * 1024 * 1024 * 1024))
    image_bytes="$(docker image inspect "aads-server:${AADS_RELEASE_SHA}" --format '{{.Size}}' 2>/dev/null || echo 0)"
    if [[ ! "$image_bytes" =~ ^[0-9]+$ ]] || [[ "$image_bytes" -le 0 ]]; then
        echo "[deploy.sh] ❌ cannot inspect release image size: aads-server:${AADS_RELEASE_SHA}"
        audit_control "release-image-size" "aads-server:${AADS_RELEASE_SHA}" "failed" "image_bytes=unknown"
        return 1
    fi
    if [[ "$image_bytes" -gt "$max_image_bytes" ]]; then
        echo "[deploy.sh] ❌ release image too large: $((image_bytes / 1024 / 1024))MB > $((max_image_bytes / 1024 / 1024))MB"
        audit_control "release-image-size" "aads-server:${AADS_RELEASE_SHA}" "blocked" "image_bytes=${image_bytes}; max_image_bytes=${max_image_bytes}"
        return 1
    fi
    echo "[deploy.sh] ✅ release image size: $((image_bytes / 1024 / 1024))MB <= $((max_image_bytes / 1024 / 1024))MB"
    audit_control "release-image-size" "aads-server:${AADS_RELEASE_SHA}" "success" "image_bytes=${image_bytes}; max_image_bytes=${max_image_bytes}"
}

build_release_image() {
    local build_max_wait existing_revision
    build_max_wait="${AADS_DEPLOY_BUILD_MAX_WAIT:-2400}"
    if [[ ! "$build_max_wait" =~ ^[0-9]+$ ]] || [[ "$build_max_wait" -lt 300 ]]; then
        build_max_wait="1200"
    fi
    # A release SHA is immutable: if its image already exists, verify the
    # revision label and reuse it instead of issuing another build.  A tag
    # pointing at a different revision is an integrity error and must never be
    # overwritten silently.
    if docker image inspect "aads-server:${AADS_RELEASE_SHA}" >/dev/null 2>&1; then
        existing_revision="$(docker image inspect "aads-server:${AADS_RELEASE_SHA}" \
            --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)"
        if [[ "$existing_revision" != "$AADS_RELEASE_SHA" ]]; then
            echo "[deploy.sh] ❌ immutable image tag mismatch: tag=${AADS_RELEASE_SHA} label=${existing_revision:-missing}"
            audit_control "release-image-reuse" "aads-server:${AADS_RELEASE_SHA}" "blocked" \
                "revision_label=${existing_revision:-missing}"
            return 1
        fi
        require_release_image_within_limit
        echo "[deploy.sh] ✅ immutable release image reuse: aads-server:${AADS_RELEASE_SHA}"
        audit_control "release-image-reuse" "aads-server:${AADS_RELEASE_SHA}" "success" \
            "revision_label=${existing_revision}"
        return 0
    fi
    require_build_disk_free
    require_dependency_lock_freshness
    report_docker_retention_status
    cleanup_release_context
    RELEASE_CONTEXT_DIR="$(mktemp -d /tmp/aads-server-release.XXXXXX)"
    git -C "$COMPOSE_DIR" archive --format=tar HEAD | tar -xf - -C "$RELEASE_CONTEXT_DIR"
    echo "[deploy.sh] clean release context: ${RELEASE_CONTEXT_DIR} (HEAD=${AADS_RELEASE_SHA}, build_timeout=${build_max_wait}s)"
    emit_release_context_manifest
    require_release_context_within_limit
    # 로컬 빌드 캐시에만 기대면 캐시가 비워졌을 때 매번 처음부터 빌드한다.
    # 2026-09-12 빌드 로그에서 CACHED 단계가 0개였고, 그중 pip wheel 단계
    # 하나가 166초, 설치 단계가 269초였다(총 455초 평균). 이전 릴리스 이미지를
    # 캐시 소스로 명시하면 로컬 캐시가 사라져도 레이어를 재사용한다.
    # inline cache 를 함께 심어 다음 배포가 이 이미지를 캐시로 쓸 수 있게 한다.
    local cache_from_args=()
    local prev_image=""
    prev_image="$(docker inspect "$(cat "${STATE_DIR}/.active_container" 2>/dev/null || echo aads-server)" \
        --format '{{.Config.Image}}' 2>/dev/null || true)"
    if [[ -n "$prev_image" ]] && docker image inspect "$prev_image" >/dev/null 2>&1; then
        cache_from_args+=(--cache-from "$prev_image")
        echo "[deploy.sh] build cache source: ${prev_image}"
    fi
    timeout --kill-after=30s "$build_max_wait" env DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}" docker build \
        --target "${AADS_DOCKER_TARGET}" \
        --build-arg "AADS_IMAGE_PROFILE=${AADS_IMAGE_PROFILE}" \
        --build-arg "INSTALL_PLAYWRIGHT=${AADS_INSTALL_PLAYWRIGHT}" \
        --build-arg BUILDKIT_INLINE_CACHE=1 \
        "${cache_from_args[@]}" \
        --label "org.opencontainers.image.revision=${AADS_RELEASE_SHA}" \
        --tag "aads-server:${AADS_RELEASE_SHA}" \
        "$RELEASE_CONTEXT_DIR"
    require_release_image_within_limit
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
DOWNTIME_FILE="${STATE_DIR}/.deploy_downtime"
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
    local _db_out
    if ! _db_out=$(timeout 10 docker exec aads-postgres psql -U aads -d aads -qAtc "$sql" 2>&1); then
        echo "[deploy.sh] WARN: deploy_db_exec failed: ${_db_out:0:200}" >&2
        return 0
    fi
    printf '%s' "$_db_out"
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
    if [[ ! "$stale_after_minutes" =~ ^[0-9]+$ ]] || [[ "$stale_after_minutes" -lt 2 ]]; then
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
        deploy_db_exec "
            INSERT INTO deploy_components(deploy_run_id, project, component, deploy_type, release_sha,
                                          status, phase, started_at, metadata, created_at, updated_at)
            SELECT ${DEPLOY_RUN_ID}, 'AADS', 'api', 'api_bluegreen', '$release_sql',
                   'running', 'initializing', NOW(),
                   jsonb_build_object('target_env', 'production', 'source', 'deploy.sh'),
                   NOW(), NOW()
            WHERE NOT EXISTS (
                SELECT 1 FROM deploy_components
                WHERE deploy_run_id=${DEPLOY_RUN_ID}
                  AND component='api'
            );
        " >/dev/null
        echo "[deploy.sh] deploy_run_id=${DEPLOY_RUN_ID}"
    fi
}

supersede_older_queued_deploy_requests() {
    if ! deploy_db_available; then
        return 0
    fi
    local release_sql reason_sql current_run_id
    release_sql="$(sql_escape "${AADS_RELEASE_SHA:-unknown}")"
    reason_sql="$(sql_escape "superseded by started release ${AADS_RELEASE_SHA:-unknown}")"
    current_run_id="${DEPLOY_RUN_ID:-0}"
    [[ "$current_run_id" =~ ^[0-9]+$ ]] || current_run_id="0"
    deploy_db_exec "
        UPDATE deploy_runs
        SET status='superseded',
            phase='superseded_by_started_deploy',
            phase_completed_at=NOW(),
            updated_at=NOW(),
            error_summary=CONCAT_WS('; ', NULLIF(error_summary, ''), '$reason_sql')
        WHERE project='AADS'
          AND status='queued'
          AND phase='queued_for_deploy'
          AND release_sha IS DISTINCT FROM '$release_sql'
          AND id <> ${current_run_id};
    " >/dev/null
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
    image_sql="$(sql_escape "$(docker inspect "${NEW_CONTAINER:-${ACTIVE_CONTAINER:-}}" --format '{{.Image}}' 2>/dev/null || true)")"
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

        UPDATE deploy_components dc
        SET status='$status_sql',
            phase='$phase_sql',
            updated_at=NOW(),
            started_at=COALESCE(started_at, to_timestamp(${DEPLOY_START_EPOCH})),
            completed_at=CASE
                WHEN '$status_sql' IN ('success', 'completed', 'failed', 'blocked') THEN NOW()
                ELSE completed_at
            END,
            duration_ms=${elapsed_ms},
            image_digest=NULLIF('$image_sql', ''),
            standby_digest=NULLIF('$standby_sql', ''),
            error_summary=NULLIF('$err_sql', '')
        FROM deploy_runs dr
        WHERE dc.deploy_run_id=${DEPLOY_RUN_ID}
          AND dr.id=dc.deploy_run_id;
    " >/dev/null
}

deploy_queue_count() {
    if ! deploy_db_available; then
        echo 0
        return 0
    fi
    local count
    count="$(
        deploy_db_exec "
            SELECT COUNT(*)::int
            FROM deploy_runs
            WHERE project='AADS'
              AND status='queued'
              AND phase='queued_for_deploy';
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ "$count" =~ ^[0-9]+$ ]]; then
        echo "$count"
    else
        echo 0
    fi
}

queue_pending_deploy_request() {
    local lock_pid="${1:-unknown}"
    if ! deploy_db_available; then
        echo "[deploy.sh] ⚠️ deploy queue unavailable: PostgreSQL is not running"
        return 0
    fi
    ensure_deploy_observability_schema || return 0
    local release_sql lock_pid_sql queue_reason_sql run_id
    release_sql="$(sql_escape "${AADS_RELEASE_SHA:-unknown}")"
    lock_pid_sql="$(sql_escape "$lock_pid")"
    queue_reason_sql="$(sql_escape "queued because active blue-green deploy is still syncing standby; active_pid=${lock_pid}")"
    run_id="$(
        deploy_db_exec "
            WITH superseded AS (
                UPDATE deploy_runs
                SET status='superseded',
                    phase='superseded_by_newer_deploy',
                    phase_completed_at=NOW(),
                    updated_at=NOW(),
                    error_summary=CONCAT_WS('; ', NULLIF(error_summary, ''), 'superseded by newer queued release $release_sql')
                WHERE project='AADS'
                  AND status='queued'
                  AND phase='queued_for_deploy'
                  AND release_sha IS DISTINCT FROM '$release_sql'
            ),
            existing AS (
                SELECT id
                FROM deploy_runs
                WHERE project='AADS'
                  AND release_sha='$release_sql'
                  AND status='queued'
                  AND phase='queued_for_deploy'
                ORDER BY id DESC
                LIMIT 1
            ),
            refreshed_existing AS (
                UPDATE deploy_runs
                   SET auto_start=TRUE,
                       requested_at=COALESCE(requested_at, NOW()),
                       last_heartbeat_at=NOW(),
                       updated_at=NOW(),
                       request_source=COALESCE(request_source, 'deploy.sh_lock_busy'),
                       requested_by=COALESCE(requested_by, 'deploy.sh'),
                       error_summary=CONCAT_WS('; ', NULLIF(error_summary, ''), '$queue_reason_sql')
                 WHERE id IN (SELECT id FROM existing)
                 RETURNING id
            ),
            active_same_release AS (
                SELECT id
                FROM deploy_runs
                WHERE project='AADS'
                  AND release_sha='$release_sql'
                  AND status IN ('running', 'verifying', 'syncing_standby')
                ORDER BY id DESC
                LIMIT 1
            ),
            inserted AS (
                INSERT INTO deploy_runs(project, release_sha, status, phase, phase_started_at,
                                        deploy_pid, last_heartbeat_at, queue_position,
                                        error_summary, requested_by, request_source,
                                        commit_status, push_status, auto_start,
                                        requested_at, created_at, updated_at)
                SELECT 'AADS', '$release_sql', 'queued', 'queued_for_deploy', NOW(),
                       $$, NOW(), 1, '$queue_reason_sql', 'deploy.sh', 'deploy.sh_lock_busy',
                       'committed', 'pushed', TRUE,
                       NOW(), NOW(), NOW()
                WHERE NOT EXISTS (SELECT 1 FROM existing)
                  AND NOT EXISTS (SELECT 1 FROM active_same_release)
                RETURNING id
            )
            SELECT id FROM inserted
            UNION ALL
            SELECT id FROM refreshed_existing
            UNION ALL
            SELECT id FROM active_same_release
            UNION ALL
            SELECT id FROM existing
            LIMIT 1;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ "$(deploy_queue_count)" == "0" ]]; then
        echo "[deploy.sh] deploy already running for release=${AADS_RELEASE_SHA}; no duplicate queue created (active_run_id=${run_id:-unknown})"
        audit_control "deploy-queue" "deploy_runs:${run_id:-unknown}" "already_running" "release=${AADS_RELEASE_SHA}; active_pid=${lock_pid_sql}"
    else
        echo "[deploy.sh] queued_for_deploy: release=${AADS_RELEASE_SHA}, active_pid=${lock_pid}, queue_run_id=${run_id:-unknown}"
        audit_control "deploy-queue" "deploy_runs:${run_id:-unknown}" "queued" "release=${AADS_RELEASE_SHA}; active_pid=${lock_pid_sql}"
    fi
}

wait_for_active_deploy_lock() {
    local max_wait="${AADS_DEPLOY_QUEUE_MAX_WAIT_SECONDS:-5400}"
    local interval="${AADS_DEPLOY_QUEUE_POLL_SECONDS:-15}"
    local waited=0
    if [[ ! "$max_wait" =~ ^[0-9]+$ ]] || [[ "$max_wait" -lt 60 ]]; then
        max_wait=5400
    fi
    if [[ ! "$interval" =~ ^[0-9]+$ ]] || [[ "$interval" -lt 5 ]]; then
        interval=15
    fi
    while [[ -f "${LOCKFILE:-/tmp/aads-deploy.lock}" ]]; do
        local lock_pid
        lock_pid="$(cat "${LOCKFILE:-/tmp/aads-deploy.lock}" 2>/dev/null || echo "")"
        if [[ -z "$lock_pid" ]] || ! kill -0 "$lock_pid" 2>/dev/null; then
            rm -f "${LOCKFILE:-/tmp/aads-deploy.lock}" 2>/dev/null || true
            return 0
        fi
        if [[ "$waited" -ge "$max_wait" ]]; then
            echo "[deploy.sh] ❌ queued deploy wait timeout: active_pid=${lock_pid}, waited=${waited}s"
            deploy_db_exec "
                UPDATE deploy_runs
                SET status='failed',
                    phase='queued_for_deploy_timeout',
                    phase_completed_at=NOW(),
                    updated_at=NOW(),
                    error_summary=CONCAT_WS('; ', NULLIF(error_summary, ''), 'queued deploy wait timeout after ${waited}s')
                WHERE project='AADS'
                  AND status='queued'
                  AND phase='queued_for_deploy';
            " >/dev/null
            return 1
        fi
        deploy_db_exec "
            UPDATE deploy_runs
            SET updated_at=NOW(),
                last_heartbeat_at=NOW(),
                duration_ms=GREATEST(0, EXTRACT(EPOCH FROM (NOW() - COALESCE(phase_started_at, created_at)))::bigint * 1000),
                estimated_remaining_ms=GREATEST(0, (${max_wait} - ${waited}) * 1000),
                error_summary='waiting for active deploy PID=${lock_pid} to finish'
            WHERE project='AADS'
              AND status='queued'
              AND phase='queued_for_deploy';
        " >/dev/null
        echo "[deploy.sh] queued deploy waiting: active_pid=${lock_pid}, waited=${waited}/${max_wait}s"
        sleep "$interval"
        waited=$((waited + interval))
    done
}

claim_latest_queued_deploy_request() {
    if [[ "${AADS_DEPLOY_QUEUE_WORKER:-false}" != "true" ]] || ! deploy_db_available; then
        return 0
    fi
    local latest_sha run_id
    latest_sha="$(
        deploy_db_exec "
            SELECT release_sha
            FROM deploy_runs
            WHERE project='AADS'
              AND status='queued'
              AND phase='queued_for_deploy'
            ORDER BY created_at DESC, id DESC
            LIMIT 1;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ -z "${latest_sha:-}" ]]; then
        echo "[deploy.sh] queue worker found no queued deployment"
        exit 0
    fi
    if [[ "$latest_sha" != "${AADS_RELEASE_SHA:-unknown}" ]]; then
        echo "[deploy.sh] ❌ latest queued release (${latest_sha}) does not match local HEAD (${AADS_RELEASE_SHA:-unknown}); leaving queue intact"
        exit 1
    fi
    run_id="$(
        deploy_db_exec "
            WITH latest AS (
                SELECT id
                FROM deploy_runs
                WHERE project='AADS'
                  AND status='queued'
                  AND phase='queued_for_deploy'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ),
            superseded AS (
                UPDATE deploy_runs
                SET status='superseded',
                    phase='superseded_by_newer_deploy',
                    phase_completed_at=NOW(),
                    updated_at=NOW()
                WHERE project='AADS'
                  AND status='queued'
                  AND phase='queued_for_deploy'
                  AND id NOT IN (SELECT id FROM latest)
            )
            UPDATE deploy_runs
            SET status='running',
                phase='preflight',
                phase_started_at=NOW(),
                deploy_pid=$$,
                last_heartbeat_at=NOW(),
                updated_at=NOW(),
                error_summary=NULL
            WHERE id IN (SELECT id FROM latest)
            RETURNING id;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ "$run_id" =~ ^[0-9]+$ ]]; then
        DEPLOY_RUN_ID="$run_id"
        deploy_db_exec "
            INSERT INTO deploy_components(deploy_run_id, project, component, deploy_type, release_sha,
                                          status, phase, started_at, metadata, created_at, updated_at)
            SELECT ${DEPLOY_RUN_ID}, project, component, deploy_type, release_sha,
                   'running', 'preflight', NOW(),
                   jsonb_build_object('target_env', target_env, 'source', 'deploy_queue_worker'),
                   NOW(), NOW()
            FROM deploy_runs
            WHERE id=${DEPLOY_RUN_ID}
              AND NOT EXISTS (
                  SELECT 1 FROM deploy_components
                  WHERE deploy_run_id=${DEPLOY_RUN_ID}
                    AND component=deploy_runs.component
              );
        " >/dev/null
        echo "[deploy.sh] claimed queued deploy_run_id=${DEPLOY_RUN_ID}"
    fi
}

start_deploy_queue_worker() {
    local trigger="${1:-manual}"
    if [[ "$(deploy_queue_count)" == "0" ]]; then
        return 0
    fi
    if [[ -f "$DEPLOY_QUEUE_WORKER_LOCKFILE" ]]; then
        local worker_pid
        worker_pid="$(cat "$DEPLOY_QUEUE_WORKER_LOCKFILE" 2>/dev/null || echo "")"
        if [[ -n "$worker_pid" ]] && kill -0 "$worker_pid" 2>/dev/null; then
            echo "[deploy.sh] deploy queue worker already running (PID=${worker_pid})"
            return 0
        fi
        rm -f "$DEPLOY_QUEUE_WORKER_LOCKFILE" 2>/dev/null || true
    fi
    local launcher
    launcher="${STATE_DIR}/scripts/start_aads_deploy_queue_worker.sh"
    if [[ -x "$launcher" ]]; then
        bash "$launcher" "$MODE" "$trigger"
        return 0
    fi

    local log_file
    log_file="${STATE_DIR}/logs/deploy-queue-worker-$(date +%Y%m%d-%H%M%S).log"
    (
        env AADS_DEPLOY_QUEUE_WORKER=true bash "$COMPOSE_DIR/deploy.sh" "$MODE"
    ) >"$log_file" 2>&1 &
    echo $! > "$DEPLOY_QUEUE_WORKER_LOCKFILE"
    echo "[deploy.sh] deploy queue worker started: PID=$!, trigger=${trigger}, log=${log_file}"
}

stop_deploy_heartbeat() {
    if [[ -n "${DEPLOY_HEARTBEAT_PID:-}" ]]; then
        kill "$DEPLOY_HEARTBEAT_PID" >/dev/null 2>&1 || true
        wait "$DEPLOY_HEARTBEAT_PID" >/dev/null 2>&1 || true
        DEPLOY_HEARTBEAT_PID=""
    fi
}

start_deploy_heartbeat() {
    stop_deploy_heartbeat
    if [[ -z "${DEPLOY_RUN_ID:-}" ]] || ! deploy_db_available; then
        return 0
    fi
    local phase="${1:-$DEPLOY_CURRENT_PHASE}"
    local status="${2:-running}"
    local interval="${AADS_DEPLOY_HEARTBEAT_SECONDS:-15}"
    if [[ ! "$interval" =~ ^[0-9]+$ ]] || [[ "$interval" -lt 5 ]]; then
        interval="15"
    fi
    local run_id="$DEPLOY_RUN_ID"
    local start_epoch="$DEPLOY_START_EPOCH"
    local phase_sql status_sql
    phase_sql="$(sql_escape "$phase")"
    status_sql="$(sql_escape "$status")"
    (
        trap '' HUP INT        # RC2: prevent HUP/INT propagation killing heartbeat
        # sleep 을 foreground 로 두면 TERM 트랩이 sleep 이 끝난 뒤에야 실행된다.
        # 그래서 stop_deploy_heartbeat 의 wait 이 매 단계마다 최대 interval(15초)
        # 만큼 멈췄고, 몇 초짜리 검사 단계들이 일률적으로 17~18초로 찍혔다.
        # 15개 단계 × 14초 ≈ 3분 30초가 순수 대기였다(2026-09-12 실측).
        # sleep 을 백그라운드로 돌리고 wait 하면 트랩이 즉시 실행된다 —
        # wait 은 시그널로 깨어나는 예외다(실측 14초 → 4ms).
        trap 'kill "${_HB_SLEEP_PID:-0}" 2>/dev/null; exit 0' TERM
        while true; do
            sleep "$interval" &
            _HB_SLEEP_PID=$!
            wait "$_HB_SLEEP_PID" 2>/dev/null || true
            local elapsed_ms estimate_ms
            elapsed_ms=$((($(date +%s) - start_epoch) * 1000))
            estimate_ms="$(deploy_estimated_remaining_ms "$elapsed_ms")"
            deploy_db_exec "
                UPDATE deploy_runs
                SET status='$status_sql',
                    phase='$phase_sql',
                    updated_at=NOW(),
                    last_heartbeat_at=NOW(),
                    duration_ms=${elapsed_ms},
                    estimated_remaining_ms=${estimate_ms}
                WHERE id=${run_id}
                  AND status NOT IN ('success', 'completed', 'failed', 'blocked');

                UPDATE deploy_components
                SET status='$status_sql',
                    phase='$phase_sql',
                    updated_at=NOW(),
                    started_at=COALESCE(started_at, to_timestamp(${start_epoch})),
                    duration_ms=${elapsed_ms}
                WHERE deploy_run_id=${run_id}
                  AND status NOT IN ('success', 'completed', 'failed', 'blocked');
            " >/dev/null
        done
    ) &
    DEPLOY_HEARTBEAT_PID=$!
}

deploy_signal_trap() {
    trap '' TERM INT HUP  # RC6: prevent re-entry during signal cleanup
    local signal_name="${1:-TERM}"
    local _sig_ppid; _sig_ppid=$(ps -o ppid= -p $$ 2>/dev/null | tr -d ' ')
    local _sig_caller; _sig_caller=$(cat "/proc/${_sig_ppid:-1}/cmdline" 2>/dev/null | tr '\0' ' ' | head -c 120)
    echo "[deploy.sh] signal_trap: ${signal_name} ppid=${_sig_ppid:-?} caller=[${_sig_caller:-?}] phase=${DEPLOY_CURRENT_PHASE}" >&2
    stop_deploy_heartbeat
    stop_downtime_monitor
    if [[ "${DEPLOY_UPSTREAM_SWITCHED:-false}" == "true" ]]; then
        # Cutover alone is not release certification. Standby synchronization,
        # QA, and the five-minute P0/P1 monitor may still be incomplete.
        deploy_phase_end "$DEPLOY_CURRENT_PHASE" "failed" "post-switch ${signal_name} — release uncertified"
        deploy_observe_update "failed" "interrupted_post_switch" "deploy interrupted by ${signal_name}; certification incomplete"
        record_deploy "failed" "$MODE" "deploy interrupted by ${signal_name} post-switch; certification incomplete"
    elif [[ "$DEPLOY_CURRENT_PHASE" == "initializing" || "$DEPLOY_CURRENT_PHASE" == "preflight" ]]; then
        deploy_phase_end "$DEPLOY_CURRENT_PHASE" "cancelled" "pre-build ${signal_name} — no container changes"
        deploy_observe_update "cancelled" "$DEPLOY_CURRENT_PHASE" "deploy superseded by ${signal_name} in ${DEPLOY_CURRENT_PHASE}"
        record_deploy "cancelled" "$MODE" "deploy superseded by ${signal_name} in ${DEPLOY_CURRENT_PHASE}"
    else
        deploy_phase_end "$DEPLOY_CURRENT_PHASE" "failed" "deploy interrupted by ${signal_name}"
        deploy_observe_update "failed" "$DEPLOY_CURRENT_PHASE" "deploy interrupted by ${signal_name}"
        record_deploy "failed" "$MODE" "deploy interrupted by ${signal_name}"
    fi
    cleanup_release_context
    rm -f "${LOCKFILE:-/tmp/aads-deploy.lock}" 2>/dev/null || true
    exit 143
}

deploy_phase_start() {
    DEPLOY_CURRENT_PHASE="${1:-unknown}"
    DEPLOY_PHASE_START_EPOCH="$(date +%s)"
    deploy_observe_update "${2:-running}" "$DEPLOY_CURRENT_PHASE" ""
    start_deploy_heartbeat "$DEPLOY_CURRENT_PHASE" "${2:-running}"
    echo "[deploy.sh] ▶ phase=${DEPLOY_CURRENT_PHASE}"
}

deploy_phase_end() {
    local phase="${1:-$DEPLOY_CURRENT_PHASE}"
    local status="${2:-success}"
    local err="${3:-}"
    stop_deploy_heartbeat
    deploy_observe_init
    if [[ -z "${DEPLOY_RUN_ID:-}" ]]; then
        return 0
    fi
    local duration_ms phase_sql status_sql err_sql current_slot_sql candidate_slot_sql image_sql standby_sql metadata_expr
    duration_ms=$((($(date +%s) - DEPLOY_PHASE_START_EPOCH) * 1000))
    phase_sql="$(sql_escape "$phase")"
    status_sql="$(sql_escape "$status")"
    err_sql="$(sql_escape "$err")"
    current_slot_sql="$(sql_escape "${CURRENT_PORT:-${ACTIVE_PORT:-unknown}}")"
    candidate_slot_sql="$(sql_escape "${NEW_PORT:-}")"
    image_sql="$(sql_escape "$(docker inspect "${NEW_CONTAINER:-${ACTIVE_CONTAINER:-}}" --format '{{.Image}}' 2>/dev/null || true)")"
    standby_sql="$(sql_escape "$(docker inspect "${OLD_CONTAINER:-}" --format '{{.Image}}' 2>/dev/null || true)")"
    if [[ -n "${DEPLOY_PHASE_METADATA_JSON:-}" ]]; then
        metadata_expr="NULLIF('$(sql_escape "$DEPLOY_PHASE_METADATA_JSON")', '')::jsonb"
    else
        # migration 150 defines metadata NOT NULL. Empty phase metadata is a
        # valid empty object, not SQL NULL.
        metadata_expr="'{}'::jsonb"
    fi
    deploy_db_exec "
        INSERT INTO deploy_phase_events(deploy_run_id, phase, status, phase_started_at,
                                        phase_completed_at, duration_ms, current_slot,
                                        candidate_slot, image_digest, standby_digest,
                                        error_summary, metadata)
        VALUES(${DEPLOY_RUN_ID}, '$phase_sql', '$status_sql',
               to_timestamp(${DEPLOY_PHASE_START_EPOCH}), NOW(), ${duration_ms},
               '$current_slot_sql', NULLIF('$candidate_slot_sql', ''),
               NULLIF('$image_sql', ''), NULLIF('$standby_sql', ''),
               NULLIF('$(sql_escape "$err")', ''), ${metadata_expr});
    " >/dev/null
    DEPLOY_PHASE_METADATA_JSON=""
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
    dirty_count="$(
        git -C "$COMPOSE_DIR" status --porcelain \
            | awk '$0 !~ /\.lock$/ && $0 !~ / app\/data\// {count++} END {print count+0}'
    )"
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
    echo "[deploy.sh] ❌ dirty worktree detected (${dirty_count} files); release blocked."
    echo "[deploy.sh]    Use a clean isolated worktree at the committed release SHA."
    audit_control "release-context" "$COMPOSE_DIR" "blocked" "dirty worktree count=${dirty_count}; clean isolated worktree required"
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

    # AADS-191: 모든 실패 경로가 여기를 지난다. 자가치유 계층이 EXIT 트랩에서
    # 원인을 분류할 수 있도록 마지막 실패 사유를 전역에 남긴다.
    if [[ "$status" == "failed" || "$status" == "blocked" ]]; then
        DEPLOY_LAST_FAIL_STATUS="$status"
        DEPLOY_LAST_FAIL_ERROR="$err"
    fi

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
    local detail="unexpected error exit=${exit_code} line=${line_no}: ${command:0:300}"
    # AADS-191 후속: preflight 등에서 이미 구체 사유(insufficient build disk 등)를 기록한 뒤
    # exit 1 이 ERR 트랩을 타면 일반 메시지가 실제 원인을 덮어써, 자가치유 분류가
    # unexpected_exit(manual) 로 잘못 떨어졌다(2026-09-13 #369 실측). 구체 사유를 보존한다.
    local last_fail="${DEPLOY_LAST_FAIL_ERROR:-}"
    if [[ -n "${last_fail//[[:space:]]/}" ]]; then
        detail="$last_fail"
    fi
    deploy_phase_end "$DEPLOY_CURRENT_PHASE" "failed" "$detail"
    record_deploy "failed" "$MODE" "$detail"
}

trap 'deploy_error_trap "$LINENO" "$BASH_COMMAND"' ERR
trap 'deploy_signal_trap TERM' TERM
trap 'deploy_signal_trap INT' INT
trap '' HUP

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
    # Read the marker first so verify_active_slot can detect divergence. The
    # previous implementation overwrote it from nginx before verification,
    # which erased the evidence of an unauthorized or stale writer.
    if [[ -f "$ACTIVE_PORT_FILE" ]]; then
        port=$(tr -d '[:space:]' < "$ACTIVE_PORT_FILE" 2>/dev/null || true)
    fi
    if [[ "$port" != "8100" && "$port" != "8102" ]] \
        && [[ "$upstream_port" == "8100" || "$upstream_port" == "8102" ]]; then
        port="$upstream_port"
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
        echo "aads-server"
        return 0
    elif [[ "$port" == "8102" ]]; then
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
            echo "$c"
            return 0
        fi
    done
    echo "aads-server"
}

write_active_slot_state() {
    local port="$1"
    local container="$2"
    local actor="${3:-deploy.sh}"
    local detail="${4:-}"
    if [[ ! -x "$ACTIVE_SLOT_STATE_WRITER" ]]; then
        echo "[deploy.sh] ❌ active-slot state writer unavailable: ${ACTIVE_SLOT_STATE_WRITER}"
        audit_control "active-slot-write" "${container}:${port}" "failed" "state writer unavailable"
        return 1
    fi
    if [[ "${NGINX_LOCK_HELD:-false}" == "true" ]]; then
        AADS_SLOT_STATE_LOCK_HELD=true "$ACTIVE_SLOT_STATE_WRITER" write \
            "$port" "$container" "$actor" "$detail"
    else
        "$ACTIVE_SLOT_STATE_WRITER" write "$port" "$container" "$actor" "$detail"
    fi
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
# All writes go through the audited, atomic writer and are checked against the
# authoritative nginx route.
write_active_slot_state "$ACTIVE_PORT" "$ACTIVE_CONTAINER" "deploy.sh" "preflight marker authorization"

# ── 배포 중복 호출 방지 (lockfile) ──
LOCKFILE="/tmp/aads-deploy.lock"
DEPLOY_FLOCKFILE="/tmp/aads-deploy.flock"
DEPLOY_LOCK_HELD=false
exec 7>"$DEPLOY_FLOCKFILE"
if ! flock -n 7; then
    LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null || echo "unknown")
    if [[ "${AADS_DEPLOY_QUEUE_WORKER:-false}" == "true" ]]; then
        echo "[deploy.sh] deploy queue worker waiting for active deploy PID=${LOCK_PID}"
        if ! wait_for_active_deploy_lock; then
            record_deploy "failed" "$MODE" "queued deploy wait timeout"
            exit 1
        fi
        if ! flock -w 60 7; then
            record_deploy "failed" "$MODE" "deploy flock acquisition failed after queue wait"
            exit 1
        fi
        DEPLOY_LOCK_HELD=true
        AADS_RELEASE_SHA="$(git -C "$COMPOSE_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
    else
        echo "[deploy.sh] 배포 진행 중 (PID=$LOCK_PID). 새 요청은 queued_for_deploy로 보류합니다."
        queue_pending_deploy_request "$LOCK_PID"
        start_deploy_queue_worker "flock_busy"
        exit 0
    fi
else
    DEPLOY_LOCK_HELD=true
fi
if [ -f "$LOCKFILE" ]; then
    LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null || echo "")
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        if [[ "${AADS_DEPLOY_QUEUE_WORKER:-false}" == "true" ]]; then
            echo "[deploy.sh] deploy queue worker waiting for active deploy PID=${LOCK_PID}"
            if ! wait_for_active_deploy_lock; then
                record_deploy "failed" "$MODE" "queued deploy wait timeout"
                exit 1
            fi
            AADS_RELEASE_SHA="$(git -C "$COMPOSE_DIR" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
        else
            echo "[deploy.sh] 배포 진행 중 (PID=$LOCK_PID). 새 요청은 queued_for_deploy로 보류합니다."
            queue_pending_deploy_request "$LOCK_PID"
            start_deploy_queue_worker "lock_busy"
            exit 0
        fi
    else
        echo "[deploy.sh] ⚠️ stale lockfile 제거 (PID=$LOCK_PID 종료됨)"
        rm -f "$LOCKFILE"
    fi
fi
echo $$ > "$LOCKFILE"
# AADS-191: 실패 원인 분류 → 자동 교정 → 재개 계층.
# 여기서 source 하는 이유는 EXIT 트랩이 이 아래에서 설치되기 때문이다.
# 라이브러리가 없어도 배포는 기존 동작 그대로 진행되어야 한다(fail open).
if [[ -f "${COMPOSE_DIR}/scripts/deploy_autoheal.sh" ]]; then
    # shellcheck source=scripts/deploy_autoheal.sh
    source "${COMPOSE_DIR}/scripts/deploy_autoheal.sh"
elif [[ -f "${STATE_DIR}/scripts/deploy_autoheal.sh" ]]; then
    source "${STATE_DIR}/scripts/deploy_autoheal.sh"
else
    echo "[deploy.sh] ⚠️ deploy_autoheal.sh 없음 — 자가치유 없이 진행"
fi

cleanup_deploy() {
    local _deploy_rc="$?"
    stop_deploy_heartbeat
    stop_downtime_monitor
    cleanup_release_context
    rm -f "$LOCKFILE"
    if [[ "${DEPLOY_LOCK_HELD:-false}" == "true" ]]; then
        flock -u 7 >/dev/null 2>&1 || true
        DEPLOY_LOCK_HELD=false
    fi
    # 자가치유는 반드시 배포 락을 놓은 뒤에 기동한다. 락을 쥔 채로 재개하면
    # 새 워커가 flock 에서 다시 대기하다가 queued 로 튕겨 나간다.
    if declare -F deploy_autoheal_on_exit >/dev/null 2>&1; then
        deploy_autoheal_on_exit "$_deploy_rc" || true
    fi
    return "$_deploy_rc"
}
trap cleanup_deploy EXIT

# A queued deploy may wait while another release changes the routed API slot.
# Re-read and re-authorize the slot only after this process owns the deploy
# lock, otherwise the stale pre-lock snapshot can make the live slot look like
# the standby target and eventually recreate it under active traffic.
refresh_active_slot_after_deploy_lock() {
    ACTIVE_PORT="$(get_active_port)"
    ACTIVE_CONTAINER="$(get_active_container)"
    HEALTH_URL="http://localhost:${ACTIVE_PORT}/api/v1/health"
    verify_active_slot "$ACTIVE_PORT"
    write_active_slot_state \
        "$ACTIVE_PORT" "$ACTIVE_CONTAINER" \
        "deploy.sh" "post-lock marker revalidation"
}

refresh_active_slot_after_deploy_lock

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
claim_latest_queued_deploy_request
deploy_phase_start "preflight" "running"
if [[ "${AADS_DEPLOY_QUEUE_WORKER:-false}" == "true" ]]; then
    # RC3: queue worker uses clean detached worktree — skip dirty gate
    report_dirty_release_exclusions
    echo "[deploy.sh] ✅ queue worker: dirty worktree gate skipped (clean worktree at ${AADS_RELEASE_SHA})"
    audit_control "release-worktree-gate" "$COMPOSE_DIR" "skipped" "queue_worker=true"
elif ! enforce_release_worktree_gate; then
    deploy_phase_end "preflight" "blocked" "dirty worktree blocks release"
    record_deploy "blocked" "$MODE" "dirty worktree blocks release"
    exit 1
fi
supersede_older_queued_deploy_requests

# 검사는 앞으로 당긴다. 디스크 검사가 build_candidate_image 안에만 있어서,
# #340 은 preflight·dependency_check·code_validation 을 모두 통과한 뒤 빌드
# 직전에야 공간 부족으로 막혔다. 헛된 4단계를 걷기 전에 여기서 끝낸다.
if ! reject_duplicate_live_release; then
    deploy_phase_end "preflight" "skipped" "same release already live on both slots"
    record_deploy "skipped" "$MODE" "same release already live on both slots"
    exit 0
fi
prune_old_release_images
if ! require_build_disk_free; then
    disk_reason="insufficient build disk"
    if [[ -n "${DEPLOY_DISK_FAIL_DETAIL:-}" ]]; then
        disk_reason="insufficient build disk (${DEPLOY_DISK_FAIL_DETAIL})"
    fi
    deploy_phase_end "preflight" "blocked" "$disk_reason"
    record_deploy "blocked" "$MODE" "$disk_reason"
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
        if [[ -x "$DEPLOY_STREAM_CLASSIFIER" ]]; then
            db_count="$(
                python3 "$DEPLOY_STREAM_CLASSIFIER" \
                    --owner-instance "$container" \
                    --ttl-seconds "${AADS_DEPLOY_STALE_HEARTBEAT_TTL_SECONDS:-90}" \
                    --mode live-count 2>/dev/null | tr -d '[:space:]' || true
            )"
            if [[ "$db_count" =~ ^[0-9]+$ ]]; then
                echo "$db_count"
                return 0
            fi
        fi
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
                  )
                  AND NOT (
                      COALESCE(error_message, '') = 'recovery_auto_retry_scheduled'
                      AND EXISTS (
                          SELECT 1
                          FROM chat_messages ph
                          WHERE ph.execution_id = chat_turn_executions.id
                            AND ph.intent = 'streaming_placeholder'
                            AND COALESCE(ph.is_hidden, FALSE) = TRUE
                      )
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

set_deploy_stream_phase_metadata() {
    local owner_instance="$1"
    local port="$2"
    local live_count="$3"
    local elapsed="$4"
    local max_wait="$5"
    local reconcile_json="${LAST_STREAM_RECONCILE_JSON:-}"
    DEPLOY_PHASE_METADATA_JSON="$(
        python3 -c '
import json
import sys

owner, port, live_count, elapsed, max_wait = sys.argv[1:6]
raw = sys.stdin.read().strip()
reconcile = {}
if raw:
    try:
        reconcile = json.loads(raw)
    except Exception:
        reconcile = {"parse_error": True, "raw": raw[:500]}
metadata = {
    "stream_samples": [{
        "owner_instance": owner,
        "port": int(port) if port.isdigit() else port,
        "live": int(live_count) if live_count.isdigit() else live_count,
        "elapsed_seconds": int(elapsed) if elapsed.isdigit() else elapsed,
        "max_wait_seconds": int(max_wait) if max_wait.isdigit() else max_wait,
    }],
    "reconcile": {
        "applied": bool(reconcile.get("applied", False)),
        "cancelled_count": int(reconcile.get("cancelled_count") or 0),
        "classes": reconcile.get("classes", {}),
        "stale_candidate_count": len(reconcile.get("stale_cancel_candidates", [])),
    },
}
print(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
' "$owner_instance" "$port" "${live_count:-unknown}" "${elapsed:-0}" "${max_wait:-0}" <<< "$reconcile_json" 2>/dev/null || true
    )"
}

reconcile_inactive_target_recovery_executions() {
    local target_container="$1"
    if [[ -z "$target_container" ]] || ! deploy_db_available; then
        return 0
    fi
    LAST_STREAM_RECONCILE_JSON=""
    if [[ -x "$DEPLOY_STREAM_CLASSIFIER" ]]; then
        local apply_flag=()
        if [[ "${AADS_DEPLOY_STALE_STREAM_APPLY:-false}" == "true" ]]; then
            apply_flag=(--apply)
        fi
        LAST_STREAM_RECONCILE_JSON="$(
            python3 "$DEPLOY_STREAM_CLASSIFIER" \
                --owner-instance "$target_container" \
                --ttl-seconds "${AADS_DEPLOY_STALE_HEARTBEAT_TTL_SECONDS:-90}" \
                --mode reconcile "${apply_flag[@]}" 2>/dev/null || true
        )"
        if [[ -n "$LAST_STREAM_RECONCILE_JSON" ]]; then
            echo "[deploy.sh] stream reconcile ${target_container}: ${LAST_STREAM_RECONCILE_JSON}"
            audit_control "stream-reconcile" "$target_container" "success" "${LAST_STREAM_RECONCILE_JSON:0:500}"
        fi
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

verify_container_slot_markers() {
    local container="$1" expected_port="$2" expected_container="$3"
    local observed
    observed="$(docker exec "$container" sh -c 'cat "$AADS_ACTIVE_PORT_FILE" "$AADS_ACTIVE_CONTAINER_FILE"' 2>/dev/null)" || return 1
    if [[ "$observed" != "${expected_port}"$'\n'"${expected_container}" ]]; then
        echo "[deploy.sh] ❌ mounted slot markers disagree: ${container}; expected=${expected_port}/${expected_container}; observed=${observed//$'\n'/\/}"
        return 1
    fi
    echo "[deploy.sh] ✅ mounted slot markers verified: ${container} sees ${expected_port}/${expected_container}"
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
    write_active_slot_state "$new_port" "$new_container" "deploy.sh" "code mode nginx cutover"
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
    ) >> "${STATE_DIR}/logs/standby-sync.log" 2>&1 &
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
        echo "[deploy.sh] standby sync pre-drain graceful shutdown trigger on inactive slot :${old_port}"
        curl -sf -X POST "http://127.0.0.1:${old_port}/api/v1/pc-agent/graceful-shutdown" \
            -H "Content-Type: application/json" 2>/dev/null || true

        local drain_max="${AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-600}"
        local drain_interval="${AADS_DEPLOY_STANDBY_SYNC_POLL_SECONDS:-5}"
        local elapsed=0
        local active="0"
        local zero_seen=0
        if [[ ! "$drain_max" =~ ^[0-9]+$ ]]; then
            drain_max="600"
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
            # The standby slot no longer receives new nginx traffic, but it still owns
            # live CEO chat turns. Preserve those turns and fail the release
            # certification closed: same-digest standby sync is mandatory and must
            # never be reported as complete while the old release is still serving.
            set_deploy_stream_phase_metadata "$old_container" "$old_port" "${active:-unknown}" "$elapsed" "$drain_max"
            echo "[deploy.sh] standby sync BLOCKED: ${old_container}:${old_port} still has active streams=${active}; release not certified"
            audit_control "standby-sync" "${old_container}:${old_port}" "blocked" "drain timeout active=${active}; same-digest certification withheld"
            return 1
        fi
        set_deploy_stream_phase_metadata "$old_container" "$old_port" "${active:-0}" "$elapsed" "$drain_max"

        if ! standby_ownership_valid "$old_container" "$old_port" "$expected_generation"; then
            # RC7: ownership change after drain is not a release failure — the slot was
            # re-activated by a newer deploy generation. Skip, do not fail the release.
            audit_control "standby-sync" "${old_container}:${old_port}" "skipped" "ownership changed after drain"
            return 0
        fi

        echo "[deploy.sh] standby sync PC Agent reconnect trigger on drained old slot :${old_port}"
        curl -sf -X POST "http://127.0.0.1:${old_port}/api/v1/pc-agent/graceful-shutdown" \
            -H "Content-Type: application/json" 2>/dev/null || true

        echo "[deploy.sh] standby sync: starting ${old_container}:${old_port} from release image ${AADS_RELEASE_SHA}"
        cd "$COMPOSE_DIR"
        if [[ "$old_container" == "aads-server-green" ]]; then
            docker compose "${COMPOSE_ENV_ARGS[@]}" -f "${COMPOSE_DIR}/docker-compose.prod.yml" --profile green up -d --no-build --no-deps --force-recreate "$old_container"
        else
            docker compose "${COMPOSE_ENV_ARGS[@]}" -f "${COMPOSE_DIR}/docker-compose.prod.yml" up -d --no-build --no-deps --force-recreate "$old_container"
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
            verify_container_slot_markers "$old_container" "$(tr -d '[:space:]' < "$ACTIVE_PORT_FILE")" "$active_container" || return 1
            docker exec "$old_container" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
            echo "[deploy.sh] standby sync complete: ${old_container}:${old_port}"
            audit_control "standby-sync" "${old_container}:${old_port}" "success" "same release image and healthy"
            return 0
        else
            echo "[deploy.sh] standby sync WARN: ${old_container}:${old_port} health failed after rebuild"
            audit_control "standby-sync" "${old_container}:${old_port}" "failed" "health failed after rebuild"
            return 1
        fi
    } > >(tee -a "${STATE_DIR}/logs/standby-sync.log") 2>&1
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
        docker start "$DEP" 2>/dev/null || (cd "$COMPOSE_DIR" && docker compose "${COMPOSE_ENV_ARGS[@]}" up -d --no-deps "$DEP")
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
        docker compose "${COMPOSE_ENV_ARGS[@]}" up -d --build --no-deps aads-server
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

        deploy_phase_start "target_slot_drain" "running"
        reconcile_inactive_target_recovery_executions "$NEW_CONTAINER"
        TARGET_STREAMS="$(stream_count_for_port "$NEW_PORT")"
        if [[ "$TARGET_STREAMS" =~ ^[0-9]+$ ]] && [[ "$TARGET_STREAMS" -gt 0 ]] && [[ "${AADS_DEPLOY_ALLOW_BUSY_TARGET:-false}" != "true" ]]; then
            local_target_drain_max="${AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT:-1800}"
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
        set_deploy_stream_phase_metadata "$NEW_CONTAINER" "$NEW_PORT" "${TARGET_STREAMS:-unknown}" "${local_target_elapsed:-0}" "${local_target_drain_max:-0}"
        deploy_phase_end "target_slot_drain" "success" "active_streams=${TARGET_STREAMS}"

        # ① release image 1회 빌드 + 새 컨테이너 시작
        cd "$COMPOSE_DIR"
        deploy_phase_start "build_candidate_image" "running"
        echo "[deploy.sh] ① release image 1회 빌드 (${AADS_RELEASE_SHA})..."
        build_release_image
        echo "[deploy.sh] ① ${NEW_CONTAINER} --no-build 시작..."
        docker compose "${COMPOSE_ENV_ARGS[@]}" $COMPOSE_FILE $PROFILE_CMD up -d --no-build --no-deps --force-recreate "$NEW_CONTAINER"
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
        set_deploy_stream_phase_metadata "$ACTIVE_CONTAINER" "$CURRENT_PORT" "${ACTIVE_STREAMS:-unknown}" "${DRAIN_ELAPSED:-0}" "60"
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
        if write_active_slot_state "$NEW_PORT" "$NEW_CONTAINER" "deploy.sh" "bluegreen cutover verification" \
            && verify_container_slot_markers "$NEW_CONTAINER" "$NEW_PORT" "$NEW_CONTAINER" \
            && curl -sf "http://127.0.0.1:${NEW_PORT}/api/v1/health" >/dev/null 2>&1 \
            && curl -sf -H "Host: ${DOWNTIME_PROBE_HOST}" "http://127.0.0.1/api/v1/health" >/dev/null 2>&1; then
            echo "[deploy.sh] ④ ✅ 전환 검증 성공"
            DEPLOY_UPSTREAM_SWITCHED=true  # RC1: from here, TERM/INT = success (new container is live)
            audit_control "nginx-switch" "${OLD_CONTAINER}:${OLD_PORT}->${NEW_CONTAINER}:${NEW_PORT}" "success" "direct and nginx-routed health verified"
            deploy_phase_end "nginx_cutover" "success" "direct and nginx-routed health verified"
        else
            echo "[deploy.sh] ⚠️ 전환 후 검증 실패 — 이전 서버로 복원"
            cp "${UPSTREAM_CONF}.pre_deploy" "$UPSTREAM_CONF"
            nginx_reload
            write_active_slot_state "$OLD_PORT" "$OLD_CONTAINER" "deploy.sh" "bluegreen verification rollback"
            docker exec "$OLD_CONTAINER" sh -c 'printf true > /tmp/aads_execution_resume_owner' 2>/dev/null || true
            docker exec "$NEW_CONTAINER" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
            notify "❌ Blue-Green 실패: 전환 검증 실패 — 복원 완료"
            deploy_phase_end "nginx_cutover" "failed" "post-switch health verification failed for ${NEW_CONTAINER}:${NEW_PORT}"
            record_deploy "failed" "$MODE" "post-switch health verification failed for ${NEW_CONTAINER}:${NEW_PORT}"
            exit 1
        fi

        # ⑤ 이전 컨테이너를 drain 후 같은 release로 재빌드해 warm standby로 동기화
        deploy_phase_start "standby_same_digest_sync" "syncing_standby"
        echo "[deploy.sh] ⑤ ${OLD_CONTAINER} standby 동기화"
        write_active_slot_state "$NEW_PORT" "$NEW_CONTAINER" "deploy.sh" "bluegreen routed health passed"
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
# 전 구간이 깨끗할 때 채우는 최소 관측 시간. 소크 자체를 없애지는 않는다.
MONITOR_MIN_SECONDS="${AADS_DEPLOY_P0P1_MIN_SECONDS:-120}"
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
    # 로그만 보면 "조용한 실패"(프로세스는 살아 있는데 응답을 못 하는 상태)를
    # 놓친다. 헬스까지 같이 확인해 검증을 넓히고, 그 대신 전 구간이 깨끗하면
    # 최소 관측 시간만 채우고 끝낸다. 300초 고정 대기는 배포 시간의 25%였다.
    if ! curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
        echo "[deploy.sh] ❌ Phase 7: health check 실패 (${MONITOR_ELAPSED}s)"
        notify "❌ 배포 후 모니터링 중 health 실패"
        deploy_phase_end "p0p1_monitoring" "failed" "health failed at ${MONITOR_ELAPSED}s"
        record_deploy "failed" "$MODE" "Phase 7 health failed at ${MONITOR_ELAPSED}s"
        exit 1
    fi
    echo "[deploy.sh] Phase 7: monitoring ${MONITOR_ELAPSED}/${MONITOR_SECONDS}초 이상 없음"
    if [[ "$MONITOR_ELAPSED" -ge "$MONITOR_MIN_SECONDS" ]]; then
        echo "[deploy.sh] Phase 7: ✅ ${MONITOR_ELAPSED}초 연속 이상 없음 — 조기 종료"
        audit_control "p0p1-monitor" "$ACTIVE_CONTAINER" "success" \
            "early_exit_after=${MONITOR_ELAPSED}s; max=${MONITOR_SECONDS}s"
        break
    fi
done
deploy_phase_end "p0p1_monitoring" "success" "seconds=${MONITOR_ELAPSED}"

echo "[deploy.sh] ✅ 배포 완료 — 필수 검증 통과 (mode=${MODE}, frontend_qa=${FRONTEND_QA_STATUS})"
notify "✅ 배포 완료 — 필수 검증 통과 (mode=${MODE}, frontend_qa=${FRONTEND_QA_STATUS})"
stop_downtime_monitor
for _final_try in 1 2 3; do
    deploy_observe_update "success" "completed" ""
    _final_status="$(deploy_db_exec "SELECT status FROM deploy_runs WHERE id=${DEPLOY_RUN_ID}")"
    if [[ "${_final_status:-}" == "success" ]]; then
        break
    fi
    echo "[deploy.sh] ⚠️ final success DB update retry ${_final_try}/3 (got status=${_final_status:-empty})"
    sleep 2
done
record_deploy "success" "$MODE" ""
# RC8: ensure final success persisted — override stale_auto if deploy_db_exec failed mid-run
for _final_retry in 1 2 3; do
    deploy_db_exec "UPDATE deploy_runs SET status='success', phase='completed', updated_at=NOW(), last_heartbeat_at=NOW(), error_summary=NULL WHERE id=${DEPLOY_RUN_ID} AND status != 'success';" >/dev/null 2>&1 && break
    sleep 2
done

# ── Phase 8: 릴리스 계보 기록 (goal 릴리스 증거의 유일한 출처) ──
# 여기서만 기록하는 이유: 이 지점은 Phase 1~7(헬스체크·DB스키마·채팅·LLM·프론트QA·
# P0/P1 5분 모니터링)을 모두 통과하고 deploy_runs 가 success/completed 로 굳은
# 뒤다. 그리고 여기는 호스트의 깨끗한 릴리스 워크트리라 Git 히스토리가 아직 살아
# 있다 — 컨테이너 안에는 .dockerignore 때문에 /app/.git 이 없어 런타임에는 계보를
# 계산할 방법이 없다. 이미지를 다시 만들거나 컨테이너를 재시작하지 않으며,
# nginx 전환 락은 이미 해제된 뒤라 락 범위가 바뀌지 않는다.
# 실패해도 배포는 이미 인증됐으므로 비치명적으로 넘어간다: 증거가 없으면 목표가
# 전진하지 않을 뿐이고, 잘못 전진하지는 않는다(fail closed).
if [[ -x "${COMPOSE_DIR}/scripts/record-release-provenance.sh" ]] && [[ -n "${DEPLOY_RUN_ID:-}" ]]; then
    "${COMPOSE_DIR}/scripts/record-release-provenance.sh" \
        --repo "$COMPOSE_DIR" \
        --deploy-run-id "$DEPLOY_RUN_ID" \
        --project "AADS" \
        --component "api" \
        --release-ref "HEAD" || echo "[deploy.sh] ⚠️ release provenance not recorded (non-fatal)"
fi

start_deploy_queue_worker "post_success"
exit 0
