#!/bin/bash
# AADS 안전 배포 게이트웨이
# 사용법: deploy.sh [bluegreen|warm-deps|code|reload|build]
#   bluegreen (기본) — Blue↔Green 무중단 전환 (중단 0초, 자동 롤백, upstream 전환)
#   warm-deps — lock/profile 기반 의존성 이미지만 사전 생성. 배포·재시작 없음.
#   code/reload/build — 레거시 모드. 기본 차단 후 bluegreen으로 자동 전환.
#                      불가피한 수동 점검 때만 AADS_DEPLOY_ALLOW_LEGACY_RESTART=true 지정.
#
# 검증 6단계: 의존성→코드검증→배포→Health→DB스키마→채팅→LLM→프론트QA

set -Eeuo pipefail
# -E(errtrace): ERR 트랩을 함수와 서브셸에도 상속시킨다.
# 이것이 없으면 함수 안에서 실패해도 트랩이 호출 지점에서 발동해, 실제로 어느
# 줄에서 죽었는지 남지 않는다. 2026-09-13 실패 로그가 전부 "line=1" 이었던
# 이유이며, 그 때문에 자가치유가 원인을 분류하지 못하고 manual 로 넘겼다.
# (실측: set -E 없으면 호출 지점만, 있으면 실패한 줄이 먼저 보고된다)
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
    bluegreen|warm-deps|code|reload|build) ;;
    *)
        echo "[deploy.sh] ERROR: 알 수 없는 모드 '$MODE'. bluegreen|warm-deps|code|reload|build 사용" >&2
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
DEPLOY_MIGRATION_ERROR=""
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

release_dependency_key() {
    local source_dir="$1" dependency_file
    {
        for dependency_file in Dockerfile requirements.runtime.lock requirements.visual.lock; do
            # A release is built from committed HEAD, never from a dirty
            # working tree.  The preflight key must use that same source or an
            # unrelated local edit can make a valid warm image look missing.
            if git -C "$source_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 && \
                    git -C "$source_dir" cat-file -e "HEAD:${dependency_file}" 2>/dev/null; then
                git -C "$source_dir" show "HEAD:${dependency_file}" | sha256sum | awk '{print $1}'
            else
                sha256sum "${source_dir}/${dependency_file}" | awk '{print $1}'
            fi
        done
        printf 'profile=%s\nplaywright=%s\n' "$AADS_IMAGE_PROFILE" "$AADS_INSTALL_PLAYWRIGHT"
    } | sha256sum | awk '{print $1}'
}

prepare_release_context() {
    cleanup_release_context
    RELEASE_CONTEXT_DIR="$(mktemp -d /tmp/aads-server-release.XXXXXX)"
    git -C "$COMPOSE_DIR" archive --format=tar HEAD | tar -xf - -C "$RELEASE_CONTEXT_DIR"
    echo "[deploy.sh] clean release context: ${RELEASE_CONTEXT_DIR} (HEAD=${AADS_RELEASE_SHA})"
    emit_release_context_manifest
    require_release_context_within_limit
}

build_dependency_image() {
    local build_max_wait dependency_key dependency_image dependency_label
    build_max_wait="${AADS_DEPLOY_BUILD_MAX_WAIT:-2400}"
    if [[ ! "$build_max_wait" =~ ^[0-9]+$ ]] || [[ "$build_max_wait" -lt 300 ]]; then
        build_max_wait="1200"
    fi

    dependency_key="$(release_dependency_key "$COMPOSE_DIR")"
    dependency_image="$(release_dependency_image "$COMPOSE_DIR")"
    if docker image inspect "$dependency_image" >/dev/null 2>&1; then
        dependency_label="$(docker image inspect "$dependency_image" \
            --format '{{index .Config.Labels "io.aads.dependency-key"}}' 2>/dev/null || true)"
        if [[ "$dependency_label" != "$dependency_key" ]]; then
            echo "[deploy.sh] ❌ immutable dependency image mismatch: image=${dependency_image}" >&2
            return 1
        fi
        echo "[deploy.sh] ✅ dependency image already warm: ${dependency_image}"
        return 0
    fi

    require_build_disk_free
    require_dependency_lock_freshness
    report_docker_retention_status
    prepare_release_context
    dependency_key="$(release_dependency_key "$RELEASE_CONTEXT_DIR")"
    dependency_image="$(release_dependency_image "$RELEASE_CONTEXT_DIR")"
    echo "[deploy.sh] dependency image warm-up: ${dependency_image}"
    timeout --kill-after=30s "$build_max_wait" env DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}" docker build \
        --target runtime-deps \
        --build-arg "AADS_IMAGE_PROFILE=${AADS_IMAGE_PROFILE}" \
        --build-arg "INSTALL_PLAYWRIGHT=${AADS_INSTALL_PLAYWRIGHT}" \
        --label "io.aads.dependency-key=${dependency_key}" \
        --tag "$dependency_image" \
        "$RELEASE_CONTEXT_DIR"
    dependency_label="$(docker image inspect "$dependency_image" \
        --format '{{index .Config.Labels "io.aads.dependency-key"}}' 2>/dev/null || true)"
    if [[ "$dependency_label" != "$dependency_key" ]]; then
        echo "[deploy.sh] ❌ dependency image verification failed: ${dependency_image}" >&2
        return 1
    fi
    audit_control "dependency-image-warmup" "$dependency_image" "success" "dependency_key=${dependency_key}"
    cleanup_release_context
    echo "[deploy.sh] ✅ dependency image warm-up complete: ${dependency_image}"
}

release_dependency_image() {
    local source_dir="$1" dependency_key
    dependency_key="$(release_dependency_key "$source_dir")" || return 1
    printf 'aads-server-deps:%s\n' "${dependency_key:0:24}"
}

release_dependency_image_ready() {
    local source_dir="$1" dependency_key dependency_image dependency_label
    dependency_key="$(release_dependency_key "$source_dir")" || return 1
    dependency_image="aads-server-deps:${dependency_key:0:24}"
    dependency_label="$(docker image inspect "$dependency_image" \
        --format '{{index .Config.Labels "io.aads.dependency-key"}}' 2>/dev/null || true)"
    [[ "$dependency_label" == "$dependency_key" ]]
}

require_build_disk_free() {
    local check_path min_free_gb min_free_kb avail_kb disk_profile cold_min_free_gb
    check_path="$(build_disk_check_path)"
    cold_min_free_gb="${AADS_DEPLOY_MIN_FREE_GB:-20}"
    disk_profile="cold_dependency_build"
    if [[ -n "${AADS_DEPLOY_MIN_FREE_GB:-}" ]]; then
        min_free_gb="$cold_min_free_gb"
        disk_profile="operator_override"
    elif release_dependency_image_ready "$COMPOSE_DIR"; then
        min_free_gb="8"
        disk_profile="warm_dependency_image"
    else
        min_free_gb="$cold_min_free_gb"
    fi
    if [[ ! "$min_free_gb" =~ ^[0-9]+$ ]] || [[ "$min_free_gb" -lt 1 ]]; then
        min_free_gb="20"
        disk_profile="invalid_override_fallback"
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
        audit_control "build-disk-preflight" "$check_path" "blocked" "available_kb=${avail_kb}; required_kb=${min_free_kb}; profile=${disk_profile}"
        return 1
    fi
    DEPLOY_DISK_FAIL_DETAIL=""
    echo "[deploy.sh] ✅ build disk preflight: ${check_path} available=$((avail_kb / 1024))MB, required=$((min_free_kb / 1024))MB, profile=${disk_profile}"
    audit_control "build-disk-preflight" "$check_path" "success" "available_kb=${avail_kb}; required_kb=${min_free_kb}; profile=${disk_profile}"
}

# 배포마다 4.24GB 이미지가 쌓이는데 회수 절차가 없었다. 2026-09-12 에 7번
# 빌드하자 /var/lib/docker 여유가 33GB → 15GB 로 말라 #340 이 빌드 직전에
# 막혔다. 실행 중이 아닌 오래된 릴리스 이미지를 남길 개수만 두고 정리한다.
prune_old_release_images() {
    local keep repo in_use kept removed tag
    # 릴리스 이미지 하나가 4.26GB 다. 3개면 12.8GB 를 상시 점유한다.
    # 2026-09-13 디스크가 90%까지 차 빌드가 막혔다. 롤백 대상은 직전
    # 릴리스 하나면 충분하므로 기본을 2로 내린다(현재+직전).
    keep="${AADS_DEPLOY_KEEP_IMAGES:-2}"
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
    local build_max_wait existing_revision dependency_key dependency_image dependency_label
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
            # 사유를 전역에 올린다. 올리지 않으면 ERR 트랩이 deploy_runs 에
            # "unexpected error exit=1 line=NNNN: return 1" 한 줄만 남기고,
            # 자가치유는 그것을 unexpected_exit(manual) 로 분류한다.
            # 2026-09-21 24시간 실측: build_candidate_image 실패 17건 중 13건이
            # 이 경로로 새어나가 한 건도 자동 재개되지 않았다. 실제 사유는
            # control audit 로그에만 있었다.
            DEPLOY_LAST_FAIL_ERROR="immutable image tag mismatch: tag=${AADS_RELEASE_SHA} label=${existing_revision:-missing}"
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
    prepare_release_context
    echo "[deploy.sh] release build timeout=${build_max_wait}s"
    # 외부 cache registry가 설정된 환경은 그 캐시도 보조 입력으로 사용한다.
    # registry가 없어도 아래 content-addressed dependency image가 wheel과
    # Playwright 레이어를 보존하므로 로컬 BuildKit metadata에 의존하지 않는다.
    local cache_from_args=()
    local cache_registry="${AADS_DEPLOY_CACHE_REGISTRY:-}"
    if [[ -n "$cache_registry" ]]; then
        cache_from_args+=(--cache-from "type=registry,ref=${cache_registry}")
        echo "[deploy.sh] build cache source(registry): ${cache_registry}"
    fi

    # BuildKit cache metadata is periodically reclaimed on this host, which
    # made unchanged wheel/Playwright layers rebuild on every release.  Keep a
    # content-addressed dependency image instead.  Tagged image layers survive
    # cache pruning and are shared with the release image, so this does not
    # duplicate the 4GB runtime on disk.
    dependency_key="$(release_dependency_key "$RELEASE_CONTEXT_DIR")"
    dependency_image="$(release_dependency_image "$RELEASE_CONTEXT_DIR")"
    if ! docker image inspect "$dependency_image" >/dev/null 2>&1; then
        echo "[deploy.sh] ❌ dependency image missing: ${dependency_image}" >&2
        echo "[deploy.sh]    Run 'bash deploy.sh warm-deps' before bluegreen; release builds stay one-image-per-SHA." >&2
        audit_control "dependency-image-reuse" "$dependency_image" "blocked" "dependency image missing; warm-deps required"
        DEPLOY_LAST_FAIL_ERROR="dependency image missing: ${dependency_image}; warm-deps required"
        return 1
    fi
    dependency_label="$(docker image inspect "$dependency_image" \
        --format '{{index .Config.Labels "io.aads.dependency-key"}}' 2>/dev/null || true)"
    if [[ "$dependency_label" != "$dependency_key" ]]; then
        echo "[deploy.sh] ❌ immutable dependency image mismatch: image=${dependency_image}" >&2
        audit_control "dependency-image-reuse" "$dependency_image" "blocked" \
            "expected=${dependency_key}; actual=${dependency_label:-missing}"
        DEPLOY_LAST_FAIL_ERROR="immutable dependency image mismatch: image=${dependency_image}; expected=${dependency_key}; actual=${dependency_label:-missing}"
        return 1
    fi
    echo "[deploy.sh] ✅ dependency image reuse: ${dependency_image}"
    audit_control "dependency-image-reuse" "$dependency_image" "success" "dependency_key=${dependency_key}"

    timeout --kill-after=30s "$build_max_wait" env DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}" docker build \
        --target "${AADS_DOCKER_TARGET}" \
        --build-arg "AADS_RUNTIME_BASE=${dependency_image}" \
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

# Every release migration goes through scripts/apply_release_migrations.sh and
# the schema_migrations ledger. 2026-09-29 866df31a: this function used to pipe
# three hard-coded files into psql and nothing else, so the release's new
# migrations/20260929_goal_pause_all_paths.sql never ran and goal dispatch failed
# with "column paused_at does not exist" (deploy.new_migration_not_applied).
# The SQL comes from committed HEAD, the same tree the release image is built from.
run_release_migrations() {
    local migration_root rc=0 output
    migration_root="$(mktemp -d /tmp/aads-server-migrations.XXXXXX)"
    if ! git -C "$COMPOSE_DIR" archive --format=tar HEAD migrations scripts/apply_migration.sh \
            scripts/apply_release_migrations.sh scripts/migrations_auto_apply_baseline.txt \
            | tar -xf - -C "$migration_root"; then
        rm -rf -- "$migration_root"
        DEPLOY_MIGRATION_ERROR="cannot extract migrations from HEAD=${AADS_RELEASE_SHA}"
        echo "[deploy.sh] ❌ ${DEPLOY_MIGRATION_ERROR}"
        return 1
    fi
    output="$(bash "${migration_root}/scripts/apply_release_migrations.sh" --root "$migration_root" "$@" 2>&1)" || rc=$?
    rm -rf -- "$migration_root"
    printf '%s\n' "$output" | grep -v '^SKIP ' | sed 's/^/[deploy.sh]   migration: /' || true
    DEPLOY_MIGRATION_ERROR=""
    case "$rc" in
        0) return 0 ;;
        4) DEPLOY_MIGRATION_ERROR="destructive SQL blocked: $(printf '%s\n' "$output" | grep '^BLOCK-DESTRUCTIVE' | cut -c19- | paste -sd';' -)" ;;
        5) DEPLOY_MIGRATION_ERROR="migration failed: $(printf '%s\n' "$output" | grep '^FAILED ' | cut -c8- | head -1)" ;;
        *) DEPLOY_MIGRATION_ERROR="migration runner exit=${rc}: $(printf '%s\n' "$output" | tail -1)" ;;
    esac
    echo "[deploy.sh] ❌ ${DEPLOY_MIGRATION_ERROR}"
    return 1
}

ensure_deploy_observability_schema() {
    if ! deploy_db_available; then
        echo "[deploy.sh] ❌ PostgreSQL is not running; cannot verify deployment observability schema"
        return 1
    fi
    # deploy_runs must exist before the first phase event, so these three are
    # brought up early. They share the ledger with the full pass below, which
    # then sees them as SKIP — no file is applied twice.
    if ! run_release_migrations \
            --only migrations/150_deploy_observability_v1.sql \
            --only migrations/20260921_deploy_session_callbacks.sql \
            --only migrations/20260922_next_step_dispatch_dedupe.sql; then
        echo "[deploy.sh] ❌ deployment observability schema migration failed"
        return 1
    fi
}

# Brings the database up to the release before the candidate slot starts.
# $1=--plan only classifies and runs the destructive-SQL gate.
apply_release_schema_migrations() {
    local phase="$1"
    shift
    deploy_phase_start "$phase" "running"
    if run_release_migrations "$@"; then
        deploy_phase_end "$phase" "success" ""
        return 0
    fi
    # Old slot keeps serving; the candidate never starts against a partial schema.
    DEPLOY_LAST_FAIL_ERROR="${DEPLOY_MIGRATION_ERROR:-schema migration failed}"
    notify "❌ Blue-Green 중단: ${DEPLOY_LAST_FAIL_ERROR}"
    deploy_phase_end "$phase" "blocked" "$DEPLOY_LAST_FAIL_ERROR"
    record_deploy "blocked" "$MODE" "$DEPLOY_LAST_FAIL_ERROR"
    exit 1
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
    # deploy_runs.status 는 CHECK 제약이 걸린 열거값이다. 단계 스크립트가 쓰는
    # 표현(skipped 등)을 그대로 넣으면 UPDATE 가 통째로 실패해, "standby 미동기화"
    # 같은 사실이 원장에서 사라지고 배포가 완전 성공으로만 남는다(2026-09-14 run 418).
    case "$status" in
        skipped|deferred|partial) status="success_partial" ;;
        error) status="failed" ;;
        completed) status="success" ;;
    esac
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
                WHEN '$status_sql' IN ('success', 'success_partial', 'completed', 'failed', 'blocked', 'superseded') THEN NOW()
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
                WHEN '$status_sql' IN ('success', 'success_partial', 'completed', 'failed', 'blocked', 'superseded') THEN NOW()
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
    local release_sql lock_pid_sql queue_reason_sql run_id changed_files_json changed_files_sql
    release_sql="$(sql_escape "${AADS_RELEASE_SHA:-unknown}")"
    lock_pid_sql="$(sql_escape "$lock_pid")"
    queue_reason_sql="$(sql_escape "queued because active blue-green deploy is still syncing standby; active_pid=${lock_pid}")"
    changed_files_json="$(
        git -C "$COMPOSE_DIR" diff-tree --no-commit-id --name-only -r "${AADS_RELEASE_SHA:-HEAD}" 2>/dev/null \
            | python3 -c 'import json, sys; print(json.dumps([line.strip() for line in sys.stdin if line.strip()]))'
    )"
    changed_files_sql="$(sql_escape "${changed_files_json:-[]}")"
    run_id="$(
        deploy_db_exec "
            BEGIN;
            SELECT pg_advisory_xact_lock(hashtext('deploy-intake:AADS:api:production'));
            WITH existing AS (
                SELECT id
                FROM deploy_runs
                WHERE project='AADS'
                  AND release_sha='$release_sql'
                  AND status='queued'
                  AND phase IN ('queued_for_deploy', 'waiting_batch_predecessor')
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
                                        requested_at, created_at, updated_at,
                                        component, deploy_type, target_env, approval_policy)
                SELECT 'AADS', '$release_sql', 'queued',
                       CASE WHEN EXISTS (
                           SELECT 1 FROM deploy_runs
                           WHERE project='AADS' AND component='api' AND target_env='production'
                             AND status='queued' AND phase='queued_for_deploy'
                             AND COALESCE(auto_start, FALSE)=TRUE
                       ) THEN 'waiting_batch_predecessor' ELSE 'queued_for_deploy' END,
                       NOW(), $$, NOW(),
                       COALESCE((
                           SELECT MAX(queue_position) + 1 FROM deploy_runs
                           WHERE project='AADS' AND component='api' AND target_env='production'
                             AND status='queued'
                             AND phase IN ('queued_for_deploy', 'waiting_batch_predecessor')
                       ), 1),
                       '$queue_reason_sql', 'deploy.sh', 'deploy.sh_lock_busy',
                       'committed', 'pushed', TRUE,
                       NOW(), NOW(), NOW(), 'api', 'api_bluegreen', 'production', 'auto_if_green'
                WHERE NOT EXISTS (SELECT 1 FROM existing)
                  AND NOT EXISTS (SELECT 1 FROM active_same_release)
                RETURNING id
            ),
            queued_request AS (
                SELECT id FROM inserted
                UNION
                SELECT id FROM existing
            ),
            manifest AS (
                INSERT INTO deploy_release_manifests(
                    deploy_run_id, project, component, target_env, release_sha,
                    changed_files, commits, risk_flags, created_at
                )
                SELECT id, 'AADS', 'api', 'production', '$release_sql',
                       '$changed_files_sql'::jsonb, jsonb_build_array('$release_sql'), '[]'::jsonb, NOW()
                FROM queued_request qr
                WHERE NOT EXISTS (
                    SELECT 1 FROM deploy_release_manifests drm
                    WHERE drm.deploy_run_id=qr.id
                )
                RETURNING deploy_run_id
            )
            SELECT id FROM inserted
            UNION ALL
            SELECT id FROM refreshed_existing
            UNION ALL
            SELECT id FROM active_same_release
            UNION ALL
            SELECT id FROM existing
            LIMIT 1;
            COMMIT;
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

# queued 후보 중 다른 후보의 git 조상인 릴리스는 더 새로운 후보에 흡수된 것으로 닫는다.
# 2026-10-06: #5599(20a97a9c)가 target_drain_busy 로 막힌 뒤 autoheal 재시도 #5601 을
# 넣었지만 created_at 이 가장 이른 #5600(6a3de230, 20a97a9c 의 조상)이 ready head 로
# 먼저 배포됐고, 최신 수정이 약 20분 늦게 올라갔다. 분기된 릴리스끼리는 건드리지 않고
# created_at 순서를 유지한다. 판정 불가(객체 없음 등)는 경고만 남기고 그 후보는 건너뛴다
# — 즉 지금 동작(created_at ASC)으로 되돌아간다. 대체된 행은 failed 가 아니라 superseded 다.
supersede_queued_ancestor_releases() {
    local rows ids=() shas=() fulls=() id sha n i j rc
    rows="$(
        deploy_db_exec "
            SELECT id::text || '|' || release_sha
              FROM deploy_runs
             WHERE project='AADS'
               AND component='api'
               AND target_env='production'
               AND status='queued'
               AND phase='queued_for_deploy'
             ORDER BY created_at ASC, id ASC;
        "
    )"
    while IFS='|' read -r id sha; do
        id="$(echo "${id:-}" | tr -d '[:space:]')"
        sha="$(echo "${sha:-}" | tr -d '[:space:]')"
        [[ "$id" =~ ^[0-9]+$ ]] || continue
        ids+=("$id")
        shas+=("$sha")
        fulls+=("$(git -C "${COMPOSE_DIR:-.}" rev-parse --verify --quiet "${sha}^{commit}" 2>/dev/null || true)")
        if [[ -z "${fulls[${#fulls[@]}-1]}" ]]; then
            echo "[deploy.sh] ⚠️ queue ancestry check: commit unresolved for run=${id} sha=${sha:-empty} — keeping created_at order for this candidate"
        fi
    done <<< "$rows"

    n=${#ids[@]}
    (( n > 1 )) || return 0
    for ((i = 0; i < n; i++)); do
        [[ -n "${fulls[i]}" ]] || continue
        for ((j = 0; j < n; j++)); do
            [[ $i -ne $j && -n "${fulls[j]}" && "${fulls[i]}" != "${fulls[j]}" ]] || continue
            if git -C "${COMPOSE_DIR:-.}" merge-base --is-ancestor "${fulls[i]}" "${fulls[j]}" 2>/dev/null; then
                rc=0
            else
                rc=$?
            fi
            if [[ "$rc" -eq 0 ]]; then
                deploy_db_exec "
                    UPDATE deploy_runs
                       SET status='superseded',
                           phase='superseded_by_newer_release',
                           phase_completed_at=NOW(),
                           updated_at=NOW(),
                           error_summary=CONCAT_WS('; ', NULLIF(error_summary,''),
                               'superseded_by_newer_release: release=$(sql_escape "${shas[j]}") run=${ids[j]}')
                     WHERE id=${ids[i]}
                       AND status='queued'
                       AND phase='queued_for_deploy';
                " >/dev/null
                echo "[deploy.sh] queued ancestor superseded: run=${ids[i]} sha=${shas[i]} -> newer run=${ids[j]} sha=${shas[j]}"
                break
            elif [[ "$rc" -ne 1 ]]; then
                echo "[deploy.sh] ⚠️ queue ancestry check failed (rc=${rc}): ${shas[i]} vs ${shas[j]} — keeping created_at order"
            fi
        done
    done
}

claim_latest_queued_deploy_request() {
    if [[ "${AADS_DEPLOY_QUEUE_WORKER:-false}" != "true" ]] || ! deploy_db_available; then
        return 0
    fi
    supersede_queued_ancestor_releases
    # ready head 는 반드시 component='api' AND target_env='production' 안에서 고른다.
    # 2026-09-23: 필터가 없어 큐 맨 앞이 dashboard 행이면 api 워커가 자기 SHA 와
    # 다르다고 판단해 영구 stand down 했다(#5131~#5133 이 이렇게 흡수됐다).
    # 큐 intake(component='api' AND target_env='production' 로 queue_position·
    # waiting_batch_predecessor 를 계산하는 곳)와 같은 술어를 써야 한다.
    local latest_sha run_id
    latest_sha="$(
        deploy_db_exec "
            SELECT release_sha
            FROM deploy_runs
            WHERE project='AADS'
              AND component='api'
              AND target_env='production'
              AND status='queued'
              AND phase='queued_for_deploy'
            ORDER BY created_at ASC, id ASC
            LIMIT 1;
        " | tail -1 | tr -d '[:space:]'
    )"
    if [[ -z "${latest_sha:-}" ]]; then
        echo "[deploy.sh] queue worker found no queued deployment"
        exit 0
    fi
    if [[ "$latest_sha" != "${AADS_RELEASE_SHA:-unknown}" ]]; then
        # 배처가 고른 ready head와 이 워커 SHA가 다르면 다른 릴리스가 대표다.
        # 실패가 아니라 할 일이 없어진 것이므로 0 으로 나간다. exit 1 로 나가면
        # ERR 트랩이 "unexpected error exit=1" 로 잡아 record_deploy failed 를
        # 남긴다. 2026-09-13 #389 가 이렇게 만들어졌다 — e0aca65c 가 0ac2166c 를
        # 흡수한 정상 동작이 실패 1건으로 집계됐다.
        echo "[deploy.sh] queue worker standing down: ready head=${latest_sha}, worker HEAD=${AADS_RELEASE_SHA:-unknown}"
        exit 0
    fi
    run_id="$(
        deploy_db_exec "
            WITH ready AS (
                SELECT id
                FROM deploy_runs
                WHERE project='AADS'
                  AND component='api'
                  AND target_env='production'
                  AND status='queued'
                  AND phase='queued_for_deploy'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
            )
            UPDATE deploy_runs
            SET status='running',
                phase='preflight',
                phase_started_at=NOW(),
                deploy_pid=$$,
                last_heartbeat_at=NOW(),
                updated_at=NOW(),
                error_summary=NULL
            WHERE id IN (SELECT id FROM ready)
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

include_queued_ancestors_in_direct_release() {
    if [[ "${AADS_DEPLOY_QUEUE_WORKER:-false}" == "true" ]] || ! deploy_db_available; then
        return 0
    fi
    if [[ ! "${DEPLOY_RUN_ID:-}" =~ ^[0-9]+$ ]]; then
        echo "[deploy.sh] ⚠️ direct release inclusion skipped: deploy_run_id unavailable"
        return 0
    fi

    local queued_rows included_run_id included_sha included_sha_sql release_sha_sql
    queued_rows="$(
        deploy_db_exec "
            SELECT id::text || '|' || release_sha
              FROM deploy_runs
             WHERE project='AADS'
               AND component='api'
               AND target_env='production'
               AND status='queued'
               AND phase IN ('queued_for_deploy', 'waiting_batch_predecessor')
               AND id <> ${DEPLOY_RUN_ID}
             ORDER BY created_at ASC, id ASC;
        "
    )"
    [[ -z "${queued_rows//[[:space:]]/}" ]] && return 0

    release_sha_sql="$(sql_escape "${AADS_RELEASE_SHA:-unknown}")"
    while IFS='|' read -r included_run_id included_sha; do
        included_run_id="$(echo "${included_run_id:-}" | tr -d '[:space:]')"
        included_sha="$(echo "${included_sha:-}" | tr -d '[:space:]')"
        [[ "$included_run_id" =~ ^[0-9]+$ ]] || continue
        [[ "$included_sha" =~ ^[0-9a-fA-F]{7,64}$ ]] || continue
        if ! git -C "$COMPOSE_DIR" merge-base --is-ancestor "$included_sha" "${AADS_RELEASE_SHA:-HEAD}" 2>/dev/null; then
            echo "[deploy.sh] direct release leaves non-ancestor queued: run=${included_run_id} sha=${included_sha}"
            continue
        fi

        included_sha_sql="$(sql_escape "$included_sha")"
        deploy_db_exec "
            BEGIN;
            SELECT pg_advisory_xact_lock(hashtext('deploy-intake:AADS:api:production'));
            INSERT INTO deploy_batch_inclusions(
                representative_run_id, included_run_id, project, component,
                target_env, included_sha, representative_sha, relationship,
                compatibility_reason, resolved_by
            )
            SELECT ${DEPLOY_RUN_ID}, ${included_run_id}, 'AADS', 'api',
                   'production', '$included_sha_sql', '$release_sha_sql', 'ancestor',
                   'current_release_contains_queued_ancestor', 'deploy.sh_direct_preflight'
             WHERE EXISTS (
                       SELECT 1 FROM deploy_runs
                        WHERE id=${DEPLOY_RUN_ID}
                          AND status IN ('running','verifying','syncing_standby')
                   )
               AND EXISTS (
                       SELECT 1 FROM deploy_runs
                        WHERE id=${included_run_id}
                          AND status='queued'
                          AND phase IN ('queued_for_deploy','waiting_batch_predecessor')
                   )
            ON CONFLICT (included_run_id) DO NOTHING;
            UPDATE deploy_runs
               SET status='superseded',
                   phase='included_in_release_batch',
                   phase_completed_at=NOW(),
                   updated_at=NOW(),
                   error_summary=CONCAT_WS('; ', NULLIF(error_summary,''),
                       'included in direct deploy run ${DEPLOY_RUN_ID} after verified Git ancestry')
             WHERE id=${included_run_id}
               AND status='queued'
               AND EXISTS (
                       SELECT 1 FROM deploy_batch_inclusions
                        WHERE representative_run_id=${DEPLOY_RUN_ID}
                          AND included_run_id=${included_run_id}
                   );
            COMMIT;
        " >/dev/null
        echo "[deploy.sh] queued ancestor included: run=${included_run_id} sha=${included_sha} -> release_run=${DEPLOY_RUN_ID}"
    done <<< "$queued_rows"
}

# 이미 구동 중인 릴리스보다 낡은 SHA 를 다시 굽는 것은 회귀 배포다.
# 2026-09-29 실측: 6d20a6eb(main 대비 35 커밋 뒤)·71bf61c2(13 뒤)·f6aa0d74(10 뒤)가
# 큐 워커/autoheal 경로로 하루 13번 재시도됐다. 성공했다면 그 뒤에 머지된 커밋이
# 운영에서 사라졌다. include_queued_ancestors_in_direct_release 는 queued 조상을
# 흡수할 뿐이고 큐 워커에서는 동작하지 않으므로, 이 가드는 워커 여부와 무관하게 돈다.
#
# 같은 SHA 는 여기서 막지 않는다. 한쪽 슬롯만 올라간 상태의 같은 릴리스 재배포는
# standby 복구이며, 양쪽 모두 라이브인 경우는 reject_duplicate_live_release 가 막는다.
# 판정 불가(구동본 SHA 미상·커밋 미존재·fetch 실패)는 차단하지 않고 경고만 남긴다.
# 가짜 차단이 더 위험하다. 의도된 롤백은 AADS_DEPLOY_ALLOW_ANCESTOR_RELEASE=1.
DEPLOY_ANCESTOR_SKIP_SUMMARY=""

running_release_sha() {
    local container="${ACTIVE_CONTAINER:-}" rev="" img=""
    [[ -n "$container" ]] || return 0
    rev="$(docker inspect "$container" \
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null || true)"
    rev="$(printf '%s' "$rev" | tr -d '[:space:]')"
    if [[ ! "$rev" =~ ^[0-9a-fA-F]{7,64}$ ]]; then
        img="$(docker inspect "$container" --format '{{.Config.Image}}' 2>/dev/null || true)"
        case "$img" in
            aads-server:*) rev="${img#aads-server:}" ;;
            *) rev="" ;;
        esac
    fi
    if [[ "$rev" =~ ^[0-9a-fA-F]{7,64}$ ]]; then
        printf '%s' "$rev"
    fi
}

resolve_release_commit() {
    local sha="$1" full=""
    full="$(git -C "$COMPOSE_DIR" rev-parse --verify --quiet "${sha}^{commit}" 2>/dev/null || true)"
    if [[ -z "$full" && "${AADS_DEPLOY_ANCESTOR_GUARD_FETCH:-1}" == "1" ]]; then
        timeout 20 git -C "$COMPOSE_DIR" fetch --quiet origin >/dev/null 2>&1 || true
        full="$(git -C "$COMPOSE_DIR" rev-parse --verify --quiet "${sha}^{commit}" 2>/dev/null || true)"
    fi
    printf '%s' "$full"
}

reject_ancestor_release() {
    DEPLOY_ANCESTOR_SKIP_SUMMARY=""
    local sha running release_full running_full main_full reason="" behind reentry run_id_sql sha_sql
    sha="${AADS_RELEASE_SHA:-}"
    if [[ "${AADS_DEPLOY_ALLOW_ANCESTOR_RELEASE:-0}" == "1" ]]; then
        echo "[deploy.sh] ancestor-release guard bypassed by AADS_DEPLOY_ALLOW_ANCESTOR_RELEASE=1"
        audit_control "ancestor-release-guard" "aads-server:${sha:-unknown}" "bypassed" "operator override"
        return 0
    fi
    if [[ ! "$sha" =~ ^[0-9a-fA-F]{7,64}$ ]]; then
        echo "[deploy.sh] ⚠️ ancestor-release guard: release sha undeterminable (${sha:-empty}) — not blocking"
        audit_control "ancestor-release-guard" "aads-server:${sha:-unknown}" "warning" "release sha undeterminable"
        return 0
    fi
    running="$(running_release_sha)"
    if [[ -z "$running" ]]; then
        echo "[deploy.sh] ⚠️ ancestor-release guard: running release undeterminable (container=${ACTIVE_CONTAINER:-unknown}) — not blocking"
        audit_control "ancestor-release-guard" "aads-server:${sha}" "warning" "running release undeterminable"
        return 0
    fi
    release_full="$(resolve_release_commit "$sha")"
    running_full="$(resolve_release_commit "$running")"
    if [[ -z "$release_full" || -z "$running_full" ]]; then
        echo "[deploy.sh] ⚠️ ancestor-release guard: commit missing locally (release=${sha} found=${release_full:+yes} running=${running} found=${running_full:+yes}) — not blocking"
        audit_control "ancestor-release-guard" "aads-server:${sha}" "warning" \
            "ancestry undeterminable; running=${running}"
        return 0
    fi
    if [[ "$release_full" == "$running_full" ]]; then
        return 0
    fi
    if git -C "$COMPOSE_DIR" merge-base --is-ancestor "$release_full" "$running_full" 2>/dev/null; then
        reason="ancestor_of_running"
    else
        # 구동본의 조상은 아니지만 origin/main 에 이미 흡수됐고, 구동본을 포함하지
        # 않으며, 구동본보다 오래된 커밋이면 역시 회귀다.
        main_full="$(git -C "$COMPOSE_DIR" rev-parse --verify --quiet 'origin/main^{commit}' 2>/dev/null || true)"
        if [[ -n "$main_full" ]] \
            && git -C "$COMPOSE_DIR" merge-base --is-ancestor "$release_full" "$main_full" 2>/dev/null \
            && ! git -C "$COMPOSE_DIR" merge-base --is-ancestor "$running_full" "$release_full" 2>/dev/null; then
            local release_ts running_ts
            release_ts="$(git -C "$COMPOSE_DIR" log -1 --format=%ct "$release_full" 2>/dev/null || echo 0)"
            running_ts="$(git -C "$COMPOSE_DIR" log -1 --format=%ct "$running_full" 2>/dev/null || echo 0)"
            if [[ "$release_ts" =~ ^[0-9]+$ && "$running_ts" =~ ^[0-9]+$ && "$release_ts" -gt 0 \
                  && "$release_ts" -lt "$running_ts" ]]; then
                reason="absorbed_in_main_older_than_running"
            fi
        fi
    fi
    [[ -n "$reason" ]] || return 0

    behind="$(git -C "$COMPOSE_DIR" rev-list --count "${release_full}..${running_full}" 2>/dev/null || echo unknown)"
    reentry=1
    if deploy_db_available; then
        sha_sql="$(sql_escape "${sha:0:8}")"
        run_id_sql="${DEPLOY_RUN_ID:-}"
        [[ "$run_id_sql" =~ ^[0-9]+$ ]] || run_id_sql="-1"
        local prior
        prior="$(
            deploy_db_exec "
                SELECT COUNT(*)::int
                  FROM deploy_runs
                 WHERE project='AADS'
                   AND release_sha LIKE '${sha_sql}%'
                   AND error_summary LIKE 'ancestor_release_skipped:%'
                   AND id <> ${run_id_sql};
            " | tail -1 | tr -d '[:space:]'
        )"
        [[ "$prior" =~ ^[0-9]+$ ]] && reentry=$((prior + 1))
    fi
    DEPLOY_ANCESTOR_SKIP_SUMMARY="ancestor_release_skipped: release=${sha} running=${running} behind=${behind} reentry=${reentry} reason=${reason}"
    echo "[deploy.sh] ⏭️ ${DEPLOY_ANCESTOR_SKIP_SUMMARY}"
    audit_control "ancestor-release-guard" "aads-server:${sha}" "superseded" "$DEPLOY_ANCESTOR_SKIP_SUMMARY"
    return 1
}

# 차단은 실패가 아니라 이미 반영된 것이다. failed 로 남기면 autoheal 이 다시
# 재시도하므로 superseded 로 닫고 0 으로 나간다.
enforce_ancestor_release_guard() {
    if reject_ancestor_release; then
        return 0
    fi
    DEPLOY_CURRENT_PHASE="preflight"
    DEPLOY_PHASE_START_EPOCH="$(date +%s)"
    deploy_phase_end "preflight" "superseded" "$DEPLOY_ANCESTOR_SKIP_SUMMARY"
    record_deploy "skipped" "$MODE" "$DEPLOY_ANCESTOR_SKIP_SUMMARY"
    exit 0
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

# 배포가 사고의 증거를 지운다.
#
# 2026-09-15, 배포 중에 끊긴 채팅 두 건을 조사하려 했더니 두 번 다 앱 로그가
# 없었다. 컨테이너를 재생성하면 `docker logs` 가 통째로 사라지기 때문이다.
# 정작 로그가 가장 필요한 순간이 배포 중 사고인데, 그 순간의 로그만 없다.
#
# 컨테이너를 갈아엎기 전에 두 슬롯의 최근 로그를 파일로 떠 둔다.
archive_slot_logs() {
    local tag="${1:-phase}"
    local out_dir="${STATE_DIR}/logs/archive"
    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$out_dir" 2>/dev/null || return 0
    local c
    for c in aads-server aads-server-green; do
        docker inspect "$c" >/dev/null 2>&1 || continue
        docker logs "$c" --since 45m > "${out_dir}/${stamp}-${tag}-${c}.log" 2>&1 || true
    done
    # 90일치만 남긴다. 조사는 그 안에 끝난다.
    find "$out_dir" -name '*.log' -mtime +90 -delete 2>/dev/null || true
    echo "[deploy.sh] 슬롯 로그 보존: ${out_dir}/${stamp}-${tag}-*.log"
}

deploy_phase_start() {
    case "${1:-}" in
        nginx_cutover|standby_same_digest_sync)
            archive_slot_logs "${1}"
            ;;
    esac
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

if [[ "$MODE" == "warm-deps" ]]; then
    trap cleanup_release_context EXIT
    # Dependency preparation is not a release, but it is still a large Docker
    # build. Serialize it with blue/green so two exporters never compete for
    # disk/BuildKit state. It also has to use a clean committed archive.
    DEPLOY_FLOCKFILE="/tmp/aads-deploy.flock"
    WARM_DEPS_LOCK_WAIT="${AADS_DEPLOY_WARM_DEPS_LOCK_WAIT:-3600}"
    [[ "$WARM_DEPS_LOCK_WAIT" =~ ^[0-9]+$ ]] || WARM_DEPS_LOCK_WAIT=3600
    exec 7>"$DEPLOY_FLOCKFILE"
    if ! flock -w "$WARM_DEPS_LOCK_WAIT" 7; then
        echo "[deploy.sh] ❌ dependency warm-up lock timeout (${WARM_DEPS_LOCK_WAIT}s)" >&2
        exit 1
    fi
    if ! enforce_release_worktree_gate; then
        echo "[deploy.sh] ❌ dependency warm-up requires a clean committed worktree" >&2
        exit 1
    fi
    build_dependency_image
    exit 0
fi

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
    # 종료 핸들러는 그 자체로 새 실패가 될 수 없다. set -E(D7) 이후 ERR 트랩이
    # 함수 안까지 상속되면서, 이 함수의 `return "$_deploy_rc"` 가 다시 트랩을
    # 태워 "unexpected error exit=N line=1" 로 실제 원인을 덮어썼다.
    # 2026-09-13 #389 가 그렇게 만들어졌다 — 큐 워커가 정상적으로 물러난 것이
    # 실패 1건으로 기록됐다. 여기서는 트랩을 끄고 나간다.
    trap - ERR
    set +E
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
enforce_ancestor_release_guard
include_queued_ancestors_in_direct_release
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

# ── P0/P1 모니터 판정 (Phase 7) ──
# Phase 7 은 트래픽 전환 뒤에 돈다. 그래서 감시가 걸려도 이전 슬롯으로 자동
# 롤백하지 않는다(정책 B): 전환 직후 standby 동기화(⑤)가 이전 슬롯을 같은 신규
# 릴리스로 다시 만들어 버려, 되돌릴 이전 이미지가 슬롯에 남지 않는다. 대신
# (1) 구 슬롯 기준선으로 "기존 소음"을 걸러 오탐을 줄이고, (2) 실패하면 실제
# 운영 슬롯·SHA 를 error_summary 첫머리에 'ACTIVE=<slot>/<sha>' 로 남긴다.
# 오류 사전: deploy.p0p1_monitor_fail_but_switched.
#
# Match the log record severity, not arbitrary payload text. Pipeline/tool
# messages legitimately embed strings such as "PRIORITY: P0-CRITICAL" inside a
# warning record; the old bare ``CRITICAL`` branch treated those as release
# failures. It also missed structlog's JSON form ("level": "error").
MONITOR_PATTERN="${AADS_DEPLOY_MONITOR_PATTERN:-\"level\"[[:space:]]*:[[:space:]]*\"(error|critical)\"|level=(error|critical)([[:space:]]|$)|Traceback [(]most recent call last[)]:|^CRITICAL([[:space:]:]|$)}"
# 채팅 세션이 사용자 입력 SQL 오타(없는 테이블/컬럼 등)로 남기는 기록은
# 서버 앱 결함이 아니므로 릴리스 실패 사유에서 뺀다. 앱은 해당 기록에
# 리터럴 토큰 USER_SQL_ERROR 를 넣는다(ceo_chat_tools_db._is_user_sql_error).
# logger 이름 전체를 제외하면 그 모듈의 진짜 버그도 묻히므로 토큰 한 가지만 쓴다.
MONITOR_EXCLUDE_PATTERN="${AADS_DEPLOY_MONITOR_EXCLUDE_PATTERN:-USER_SQL_ERROR}"

# 기준선 창: 2026-10-14 실측에서 구 슬롯이 기동 후 15분간 같은 이벤트(llm_first_response_timeout)를
# 30건 냈다. 그 구간을 덮는 값. 의도적으로 환경변수로 풀지 않는다 — 감시를 느슨하게 하는
# 우회로가 되기 때문이다.
P0P1_BASELINE_WINDOW_SECONDS=900
# 구 슬롯 분당 비율 × 3 + 3 건까지는 소음. 3배는 같은 분포의 5분 표본이 흔들리는 폭(포아송
# 3σ 근처)을 덮고, +3 은 비율이 0.1건/분 미만일 때 1~2건이 곧바로 실패가 되는 것을 막는다.
# 이보다 큰 증가나 구 슬롯에 없던 이벤트 키는 신규 코드 기인으로 보고 실패시킨다.
P0P1_BASELINE_FACTOR=3
P0P1_BASELINE_SLACK=3
P0P1_BASELINE_ROWS=""
P0P1_NOISE_NOTE=""

# stdin: docker logs 전체. stdout: "<이벤트키> <건수>" 줄들.
# 키 = 구조화 로그의 event 첫 토큰(없으면 숫자를 N 으로 지운 본문). 한 줄짜리
# Traceback 은 직전 줄의 키로 묶고, 직전 줄이 이미 센 오류 기록이면 같은 사건이므로
# 다시 세지 않는다.
p0p1_event_counts() {
    local pattern="$1" exclude="$2"
    { grep -nE -B1 -- "$pattern" || true; } | { grep -Ev -- "$exclude" || true; } | awk '
        function key_of(t,   s) {
            if (match(t, /"event"[ \t]*:[ \t]*"[^" \t:|=,\\]+/)) {
                s = substr(t, RSTART, RLENGTH)
                sub(/^"event"[ \t]*:[ \t]*"/, "", s)
                return s
            }
            if (match(t, /event=[^ \t]+/)) return substr(t, RSTART + 6, RLENGTH - 6)
            return ""
        }
        function fallback_key(t,   s) {
            s = t
            gsub(/[0-9]+/, "N", s)
            gsub(/[ \t]+/, "_", s)
            return "text:" substr(s, 1, 80)
        }
        $0 == "--" { last_key = ""; last_counted = 0; next }
        {
            if (!match($0, /^[0-9]+[:-]/)) next
            sep = substr($0, RLENGTH, 1)
            text = substr($0, RLENGTH + 1)
            is_tb = (text ~ /^[ \t]*Traceback \(most recent call last\):/)
            k = is_tb ? "" : key_of(text)
            if (sep == "-") {
                last_key = k; last_counted = 0
                next
            }
            if (is_tb) {
                if (last_key == "") { c["traceback"]++ }
                else if (!last_counted) { c[last_key]++ }
                last_counted = 1
                next
            }
            if (k == "") k = fallback_key(text)
            c[k]++
            last_key = k; last_counted = 1
        }
        END { for (k in c) printf "%s %d\n", k, c[k] }
    ' | sort
}

# $1 = 기준선 행("B <키> <건수> <창초>" 줄들), $2 = 관측 행("<키> <건수>" 줄들),
# $3 = 관측 경과 초. stdout: FAIL_NEW / FAIL_INCREASE / NOISE 줄. FAIL 이 있으면 rc=1.
p0p1_classify_hits() {
    local baseline_rows="$1" observed_rows="$2" elapsed="$3" verdict
    verdict="$({
        printf '%s\n' "$baseline_rows"
        printf '%s\n' "$observed_rows" | awk 'NF >= 2 { print "O", $1, $2 }'
    } | awk -v elapsed="$elapsed" -v factor="$P0P1_BASELINE_FACTOR" -v slack="$P0P1_BASELINE_SLACK" '
        $1 == "B" && $4 > 0 {
            rate = $3 / $4 * 60
            if (!($2 in base) || rate > base[$2]) base[$2] = rate
            next
        }
        $1 == "O" { obs[$2] = $3 }
        END {
            em = elapsed / 60
            for (k in obs) {
                if (!(k in base)) {
                    printf "FAIL_NEW %s obs=%d baseline=none\n", k, obs[k]
                    continue
                }
                allowed = base[k] * em * factor + slack
                if (obs[k] > allowed)
                    printf "FAIL_INCREASE %s obs=%d allowed=%.1f baseline_per_min=%.2f\n", k, obs[k], allowed, base[k]
                else
                    printf "NOISE %s obs=%d allowed=%.1f baseline_per_min=%.2f\n", k, obs[k], allowed, base[k]
            }
        }
    ' | sort)"
    printf '%s\n' "$verdict"
    if grep -q '^FAIL_' <<<"$verdict"; then
        return 1
    fi
    return 0
}

# 컷오버 직전 구 슬롯(아직 트래픽을 받던 컨테이너)의 이벤트 비율을 모은다. ⑤ standby
# 동기화가 이 컨테이너를 신규 릴리스로 다시 만들면 로그가 사라지므로 그 전에 불러야 한다.
# 두 창의 최댓값을 쓴다: 컷오버 직전 창(정상 상태) + 기동 직후 창(재개 세션 소음).
# $1 = 컨테이너, $2 = 컷오버 epoch.
p0p1_capture_baseline() {
    local container="$1" cutover_epoch="$2"
    local window="$P0P1_BASELINE_WINDOW_SECONDS" started started_epoch win2 counts rows=""
    P0P1_BASELINE_ROWS=""
    docker inspect "$container" >/dev/null 2>&1 || return 0
    counts="$(docker logs "$container" --since "$((cutover_epoch - window))" --until "$cutover_epoch" 2>&1 \
        | p0p1_event_counts "$MONITOR_PATTERN" "$MONITOR_EXCLUDE_PATTERN" || true)"
    rows="$(awk -v w="$window" 'NF >= 2 { print "B", $1, $2, w }' <<<"$counts")"
    started="$(docker inspect "$container" --format '{{.State.StartedAt}}' 2>/dev/null || true)"
    started_epoch="$(date -d "$started" +%s 2>/dev/null || echo 0)"
    if (( started_epoch > 0 && cutover_epoch > started_epoch )); then
        win2=$((cutover_epoch - started_epoch))
        (( win2 > window )) && win2=$window
        if (( win2 >= 60 )); then
            counts="$(docker logs "$container" --since "$started_epoch" --until "$((started_epoch + win2))" 2>&1 \
                | p0p1_event_counts "$MONITOR_PATTERN" "$MONITOR_EXCLUDE_PATTERN" || true)"
            rows+=$'\n'"$(awk -v w="$win2" 'NF >= 2 { print "B", $1, $2, w }' <<<"$counts")"
        fi
    fi
    P0P1_BASELINE_ROWS="$(grep '^B ' <<<"$rows" || true)"
    echo "[deploy.sh] P0/P1 기준선 수집: ${container} rows=$(grep -c '^B ' <<<"$P0P1_BASELINE_ROWS" || true) (window=${window}s)"
}

# nginx upstream 주석(deploy.sh 가 전환마다 갱신)과 해당 컨테이너 이미지 태그로
# 실제 운영 슬롯을 읽는다. 변수(NEW_CONTAINER 등)가 아니라 실제 상태를 읽는 이유는,
# 이 값이 "무엇이 운영 중인가" 를 기록하는 용도이기 때문이다. stdout: <slot>/<sha>.
p0p1_active_descriptor() {
    local line slot port container image sha
    line="$(grep -m1 '^# Active API slot:' "$UPSTREAM_CONF" 2>/dev/null || true)"
    slot="$(sed -E 's/^# Active API slot: ([a-z]+) \(([0-9]+)\).*/\1/' <<<"$line")"
    port="$(sed -E 's/^# Active API slot: ([a-z]+) \(([0-9]+)\).*/\2/' <<<"$line")"
    if [[ -z "$line" || "$slot" == "$line" ]]; then
        echo "unknown/unknown"
        return 0
    fi
    case "$port" in
        8102) container="aads-server-green" ;;
        *)    container="aads-server" ;;
    esac
    image="$(docker inspect "$container" --format '{{.Config.Image}}' 2>/dev/null || true)"
    sha="${image##*:}"
    echo "${slot}/${sha:-unknown}"
}

# Phase 7 실패 확정. 롤백하지 않으므로 deploy_runs 가 "실패 = 운영에서 내려갔다" 로
# 읽히지 않게 실제 운영 슬롯을 첫머리에 못 박는다. status 열거값(deploy_runs_status_check)
# 에 switched_but_monitor_failed 를 추가하면 마이그레이션이 필요해 status=failed 를 유지한다.
# $1 = 사유 종류, $2 = 상세.
p0p1_monitor_fail() {
    local kind="$1" detail="$2" active summary
    active="$(p0p1_active_descriptor)"
    summary="ACTIVE=${active}; release=${AADS_RELEASE_SHA:-unknown}; switched_but_monitor_failed (롤백 안 함); ${kind}: ${detail:0:380}"
    echo "[deploy.sh] ❌ Phase 7 실패 확정 — 운영 중 ${active} (롤백하지 않음)"
    notify "❌ 배포 후 P0/P1 모니터링 실패 — 운영 중 ${active} (롤백하지 않음): ${detail:0:200}"
    deploy_phase_end "p0p1_monitoring" "failed" "$summary"
    record_deploy "failed" "$MODE" "$summary"
    exit 1
}

# ── Finance sidecar topology ─────────────────────────────────────────────────
# The finance service is NOT necessarily a local container. The approved
# placements are a closed table keyed by the route nginx really uses
# (upstream yeoljeong_finance_api). Do not make these env-overridable: an
# env switch that selects "remote" or "optional" is a way to skip the gate.
#   127.0.0.1:8210  remote  ssh -L tunnel to the APPROVED accounting host only
#                           (FINANCE_TUNNEL_APPROVED_HOST); the listener pid's
#                           real forward is resolved with `ssh -G`, never trusted
#                           from the listener name or from /health/live
#   127.0.0.1:8110  local   bind-mounted container yeoljeong-finance
#   (no proxy_pass reference) unused  upstream declared (or absent) but no active
#                           proxy_pass in the real nginx config points at it, so
#                           nothing is routed there and there is nothing to
#                           health-check. Decided from the real config, never
#                           from env; if the config cannot be read the gate fails
#                           closed. Any reference at all falls back to the
#                           local/remote rules above (jinah244 stays blocked).
# Anything else (no/multiple upstream servers, other port) is unknown and
# blocks the release. Deploy #5814 failed because a local container was
# assumed while the live route was the remote tunnel.
FINANCE_ROUTE_REMOTE_TUNNEL="127.0.0.1:8210"
FINANCE_ROUTE_LOCAL_MOUNTED="127.0.0.1:8110"
FINANCE_LOCAL_CONTAINER="yeoljeong-finance"
# The remote tunnel is only approved when it really forwards to the current
# ACCT host (cafe24_114). The retired jinah244 host (5.104.85.244) and any
# unknown host are rejected even if /health/live answers 200 there.
# FINANCE_TUNNEL_REMOTE_TARGET is the accounting finance listener behind the
# tunnel; it mirrors the target of the tunnel that was live on 2026-10-20.
FINANCE_TUNNEL_APPROVED_HOST="114.207.244.86"
FINANCE_TUNNEL_REMOTE_TARGET="127.0.0.1:8110"

# Seams for the process table; behavior tests replace them, nothing reads env.
finance_proc_cmdline() { tr '\0' '\n' < "/proc/$1/cmdline"; }
finance_ssh_resolve() { timeout 15 ssh -G "$@"; }
# Full effective nginx config as the running aads-nginx sees it (stdout only).
finance_nginx_dump() { timeout 20 docker exec aads-nginx nginx -T 2>/dev/null; }

# OpenSSH `ssh -G` prints forward endpoints bracketed ("[127.0.0.1]:8210
# [127.0.0.1]:8110"); the bare "127.0.0.1:8210" form is also accepted. Both
# normalize to host:port. A port-only bind ("8210"), a wildcard/other bind or a
# hostname such as "[localhost]" is left as-is and therefore never matches the
# strict loopback comparison below.
finance_norm_endpoint() {
    if [[ "$1" =~ ^\[([^][[:space:]]+)\]:([0-9]+)$ ]]; then
        printf '%s:%s' "${BASH_REMATCH[1],,}" "${BASH_REMATCH[2]}"
    elif [[ "$1" =~ ^([^][:[:space:]]+):([0-9]+)$ ]]; then
        printf '%s:%s' "${BASH_REMATCH[1],,}" "${BASH_REMATCH[2]}"
    else
        printf '%s' "$1"
    fi
}

# Validates, without any remote connection, that the ssh process listening on
# 127.0.0.1:$1 forwards to the approved accounting destination.
# rc 0 = approved; rc 1 + reason on stderr otherwise (fail closed).
verify_finance_tunnel_destination() {
    local port="$1" bind="127.0.0.1:$1" listener pid line
    local -a pids args
    local argv0 cfg key value host="" hops="" fwd_count=0 fwd_ok=false
    local fwd_bind fwd_target fwd_extra
    listener="$(ss -ltnpH "sport = :${port}" 2>/dev/null || true)"
    if [[ -z "${listener//[[:space:]]/}" ]]; then
        echo "finance remote route ${bind} has no listener (got: none)" >&2
        return 1
    fi
    while IFS= read -r line; do
        [[ -z "${line//[[:space:]]/}" ]] && continue
        if [[ "$line" != *"${bind} "* ]]; then
            echo "finance tunnel listener is not bound to loopback ${bind} (got: ${line})" >&2
            return 1
        fi
        if [[ "$line" != *'"ssh"'* ]]; then
            echo "finance remote route ${bind} is not owned by an ssh tunnel listener (got: ${line})" >&2
            return 1
        fi
        mapfile -t pids < <(grep -o 'pid=[0-9]*' <<< "$line" | cut -d= -f2 | sort -u)
        if (( ${#pids[@]} != 1 )); then
            echo "finance tunnel listener ${bind} must have exactly one owning pid (got: ${#pids[@]})" >&2
            return 1
        fi
        if [[ -n "${pid:-}" && "$pid" != "${pids[0]}" ]]; then
            echo "finance tunnel listener ${bind} is owned by multiple pids (${pid}, ${pids[0]})" >&2
            return 1
        fi
        pid="${pids[0]}"
    done <<< "$listener"
    [[ -n "${pid:-}" ]] || { echo "finance tunnel listener pid unresolved" >&2; return 1; }

    if ! mapfile -t args < <(finance_proc_cmdline "$pid" 2>/dev/null) || (( ${#args[@]} < 2 )); then
        echo "finance tunnel pid ${pid} command line unreadable" >&2
        return 1
    fi
    argv0="${args[0]##*/}"
    if [[ "$argv0" != "ssh" ]]; then
        echo "finance tunnel pid ${pid} is '${argv0}', not ssh" >&2
        return 1
    fi

    # ssh -G resolves aliases/Host blocks from local config only; it never connects.
    if ! cfg="$(finance_ssh_resolve "${args[@]:1}" 2>/dev/null)"; then
        echo "finance tunnel pid ${pid}: ssh -G could not resolve its destination" >&2
        return 1
    fi
    while read -r key value; do
        case "$key" in
            hostname) host="${value,,}" ;;
            proxyjump|proxycommand)
                [[ -n "$value" && "$value" != "none" ]] && hops="${key}=${value}" ;;
            localforward)
                read -r fwd_bind fwd_target fwd_extra <<< "$value"
                if [[ -n "$fwd_extra" ]]; then
                    fwd_count=$((fwd_count + 2))
                elif [[ "$(finance_norm_endpoint "$fwd_bind")" == "$bind" ]]; then
                    fwd_count=$((fwd_count + 1))
                    [[ "$(finance_norm_endpoint "$fwd_target")" == "$FINANCE_TUNNEL_REMOTE_TARGET" ]] && fwd_ok=true
                fi ;;
        esac
    done <<< "$cfg"

    if [[ -z "$host" ]]; then
        echo "finance tunnel pid ${pid}: destination host unresolved" >&2
        return 1
    fi
    if [[ -n "$hops" ]]; then
        echo "finance tunnel pid ${pid} reaches ${host} through an intermediate hop (${hops}); not an approved placement" >&2
        return 1
    fi
    if [[ "$host" != "$FINANCE_TUNNEL_APPROVED_HOST" ]]; then
        echo "finance tunnel destination ${host} is not the approved ACCT host ${FINANCE_TUNNEL_APPROVED_HOST} (cafe24_114); health 200 does not override this" >&2
        return 1
    fi
    if (( fwd_count != 1 )) || [[ "$fwd_ok" != "true" ]]; then
        echo "finance tunnel pid ${pid} must forward ${bind} to ${FINANCE_TUNNEL_REMOTE_TARGET} exactly once (forwards on ${bind}: ${fwd_count})" >&2
        return 1
    fi
    return 0
}

# Fallback when `nginx -T` is unavailable: the active conf.d files next to
# $UPSTREAM_CONF. Backup copies (.bak*, .pre*) are not live config.
finance_confd_dump() {
    local dir f found=false
    dir="$(dirname "${UPSTREAM_CONF:-/etc/nginx/conf.d/aads-upstream.conf}")"
    [[ -d "$dir" ]] || return 1
    for f in "$dir"/*.conf "$dir"/*.inc; do
        [[ -f "$f" ]] || continue
        case "${f##*/}" in *.bak*|*.pre*) continue ;; esac
        cat "$f" 2>/dev/null || return 1
        printf '\n'
        found=true
    done
    [[ "$found" == "true" ]]
}

# Counts non-comment lines of config text $1 matching ERE $2.
finance_nginx_count() {
    local n
    n="$(sed -E 's/#.*$//' <<< "$1" | grep -cE "$2" || true)"
    printf '%s' "${n:-0}"
}

FINANCE_ANY_PASS_RE='(^|[^A-Za-z0-9_])(proxy|fastcgi|grpc|uwsgi|scgi)_pass[[:space:]]'
FINANCE_UPSTREAM_REF_RE="${FINANCE_ANY_PASS_RE}[^;]*yeoljeong_finance_api([^A-Za-z0-9_]|\$)"

# Prints the effective nginx config (rc 0) or rc 1 when it cannot be judged. A
# dump without a single *_pass directive is treated as unreadable: "zero
# references" must come from a config that demonstrably contains the routing.
finance_nginx_effective_config() {
    local cfg
    if cfg="$(finance_nginx_dump)" && (( $(finance_nginx_count "$cfg" "$FINANCE_ANY_PASS_RE") > 0 )); then
        printf '%s\n' "$cfg"
        return 0
    fi
    if cfg="$(finance_confd_dump 2>/dev/null)" && (( $(finance_nginx_count "$cfg" "$FINANCE_ANY_PASS_RE") > 0 )); then
        printf '%s\n' "$cfg"
        return 0
    fi
    return 1
}

# Prints "<mode> <route>" (mode=local|remote|unused) on stdout; reason on
# stderr, rc 1. Nothing but the result may reach stderr on success: callers
# capture stdout and stderr together.
resolve_finance_sidecar_topology() {
    local conf="${UPSTREAM_CONF:-/etc/nginx/conf.d/aads-upstream.conf}"
    local -a routes
    local route listener why cfg refs
    if ! cfg="$(finance_nginx_effective_config)"; then
        echo "cannot determine whether any proxy_pass references yeoljeong_finance_api (nginx -T and active conf.d files both unusable); failing closed" >&2
        return 1
    fi
    refs="$(finance_nginx_count "$cfg" "$FINANCE_UPSTREAM_REF_RE")"
    if (( refs == 0 )); then
        routes=()
        if [[ -r "$conf" ]]; then
            mapfile -t routes < <(awk '
                /^[[:space:]]*upstream[[:space:]]+yeoljeong_finance_api[[:space:]]*\{/ {in_up=1; next}
                in_up && /^[[:space:]]*\}/ {in_up=0}
                in_up && /^[[:space:]]*server[[:space:]]+/ {
                    v=$2; sub(/;.*/, "", v); print v
                }' "$conf")
        fi
        if (( ${#routes[@]} == 0 )); then
            echo "unused none"
        else
            (IFS=','; echo "unused ${routes[*]}")
        fi
        return 0
    fi
    [[ -r "$conf" ]] || { echo "nginx upstream config unreadable: ${conf}" >&2; return 1; }
    mapfile -t routes < <(awk '
        /^[[:space:]]*upstream[[:space:]]+yeoljeong_finance_api[[:space:]]*\{/ {in_up=1; next}
        in_up && /^[[:space:]]*\}/ {in_up=0}
        in_up && /^[[:space:]]*server[[:space:]]+/ {
            v=$2; sub(/;.*/, "", v); print v
        }' "$conf")
    if (( ${#routes[@]} != 1 )); then
        echo "unknown finance topology: yeoljeong_finance_api must have exactly one active server (found ${#routes[@]})" >&2
        return 1
    fi
    route="${routes[0]}"
    case "$route" in
        "$FINANCE_ROUTE_LOCAL_MOUNTED")
            # A local route is only local if no ssh forward sits on the port.
            listener="$(ss -ltnpH "sport = :${route##*:}" 2>/dev/null || true)"
            if [[ "$listener" == *'"ssh"'* || "$listener" == *'"autossh"'* ]]; then
                echo "finance local route ${route} is owned by an ssh forward, contradicting the local placement (got: ${listener})" >&2
                return 1
            fi
            echo "local ${route}"
            ;;
        "$FINANCE_ROUTE_REMOTE_TUNNEL")
            # The remote route is only approved if the ssh forward owning the
            # port really leads to the approved accounting host.
            if ! why="$(verify_finance_tunnel_destination "${route##*:}" 2>&1)"; then
                echo "$why" >&2
                return 1
            fi
            echo "remote ${route}"
            ;;
        *)
            echo "unknown finance topology: yeoljeong_finance_api routes to ${route}, not an approved placement" >&2
            return 1
            ;;
    esac
}

# Fills FINANCE_SIDECARS with the local containers that must exist and be
# restarted (never the API itself); rc 1 + FINANCE_SIDECAR_ERROR on a forbidden name.
finance_local_sidecar_list() {
    local mode="$1" sidecar existing dup
    local -a candidates extra
    FINANCE_SIDECARS=()
    FINANCE_SIDECAR_ERROR=""
    candidates=()
    [[ "$mode" == "local" ]] && candidates+=("$FINANCE_LOCAL_CONTAINER")
    IFS=',' read -r -a extra <<< "${DEPLOY_MOUNTED_SIDECARS:-}"
    candidates+=("${extra[@]}")
    for sidecar in "${candidates[@]}"; do
        sidecar="${sidecar//[[:space:]]/}"
        [[ -z "$sidecar" ]] && continue
        if [[ ( "$mode" == "remote" || "$mode" == "unused" ) && "$sidecar" == "$FINANCE_LOCAL_CONTAINER" ]]; then
            FINANCE_SIDECAR_ERROR="refusing obsolete local finance container ${sidecar} while finance topology=${mode}"
            return 1
        fi
        if [[ "$sidecar" == aads-server* ]]; then
            FINANCE_SIDECAR_ERROR="refusing API container as mounted sidecar: ${sidecar}"
            return 1
        fi
        dup=false
        for existing in "${FINANCE_SIDECARS[@]}"; do
            [[ "$existing" == "$sidecar" ]] && dup=true
        done
        [[ "$dup" == "true" ]] || FINANCE_SIDECARS+=("$sidecar")
    done
    return 0
}

# Read-only health of the real finance route. $1 = URL, $2 = wait seconds.
finance_route_health_wait() {
    local url="$1" wait="$2" interval="${DEPLOY_SIDECAR_HEALTH_INTERVAL:-2}" elapsed=0
    [[ "$interval" =~ ^[0-9]+$ ]] || interval=2
    (( interval > 0 )) || interval=2
    until curl -sf --max-time 5 "$url" >/dev/null 2>&1; do
        (( elapsed >= wait )) && return 1
        sleep "$interval"
        elapsed=$((elapsed + interval))
    done
}

# Authenticated public route: 200/401/403 proves nginx routing + auth gate;
# 500/502/000 (broken config or dead upstream) fails.
finance_public_route_check() {
    local url="$1" code
    code="$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || echo 000)"
    case "$code" in
        200|401|403) return 0 ;;
    esac
    FINANCE_PUBLIC_CODE="$code"
    return 1
}

# Runs BEFORE the image build and before any cutover. Read-only: it never
# restarts or recreates anything. Local mode requires the container to exist;
# remote mode requires the routed upstream to answer health.
preflight_mounted_sidecar_topology() {
    local topo mode route failure="" sidecar health_wait
    health_wait="${DEPLOY_SIDECAR_PREFLIGHT_WAIT:-10}"
    [[ "$health_wait" =~ ^[0-9]+$ ]] || health_wait=10

    deploy_phase_start "sidecar_topology_preflight" "verifying"
    if ! topo="$(resolve_finance_sidecar_topology 2>&1)"; then
        failure="$topo"
    else
        mode="${topo%% *}"
        route="${topo##* }"
        if ! finance_local_sidecar_list "$mode"; then
            failure="$FINANCE_SIDECAR_ERROR"
        else
            for sidecar in "${FINANCE_SIDECARS[@]}"; do
                if ! docker inspect "$sidecar" >/dev/null 2>&1; then
                    failure="required local sidecar absent: ${sidecar} (finance topology=${mode} route=${route})"
                    break
                fi
            done
        fi
        if [[ -z "$failure" && "$mode" == "remote" ]]; then
            finance_route_health_wait "http://${route}/health/live" "$health_wait" \
                || failure="remote finance route health failed: http://${route}/health/live (waited ${health_wait}s)"
        fi
        if [[ -z "$failure" && "$mode" == "unused" ]]; then
            echo "[deploy.sh] ℹ️ finance topology=unused (route=${route}): no proxy_pass references yeoljeong_finance_api; finance health wait skipped"
        fi
    fi

    if [[ -n "$failure" ]]; then
        echo "[deploy.sh] ❌ sidecar topology preflight: ${failure}"
        deploy_phase_end "sidecar_topology_preflight" "blocked" "$failure"
        notify "❌ 배포 차단(빌드 전): ${failure}"
        return 1
    fi
    echo "[deploy.sh] ✅ sidecar topology preflight: finance=${mode} route=${route}"
    deploy_phase_end "sidecar_topology_preflight" "success" "finance topology=${mode} route=${route}"
}

# Bind-mounted local sidecars are not recreated by the API blue/green compose
# start. Reload them while holding the nginx cutover lock so a concurrent
# deploy cannot race the mounted process or certify a route against stale
# sidecar code. Remote finance is never restarted from here: only its routed
# health is verified (read-only).
reload_mounted_sidecars() {
    local sidecar internal_url public_url health_wait health_interval health_elapsed
    local topo mode route
    local failure=""

    public_url="${DEPLOY_SIDECAR_PUBLIC_CHECK_URL:-http://127.0.0.1/api/v1/yeoljeong-finance/health/live}"
    health_wait="${DEPLOY_SIDECAR_HEALTH_WAIT:-60}"
    health_interval="${DEPLOY_SIDECAR_HEALTH_INTERVAL:-2}"
    [[ "$health_wait" =~ ^[0-9]+$ ]] || health_wait=60
    [[ "$health_interval" =~ ^[0-9]+$ ]] || health_interval=2

    deploy_phase_start "mounted_sidecar_reload" "verifying"
    if ! topo="$(resolve_finance_sidecar_topology 2>&1)"; then
        failure="$topo"
    else
        mode="${topo%% *}"
        route="${topo##* }"
        internal_url="${DEPLOY_SIDECAR_CHECK_URL:-http://${route}/health/live}"
        if [[ "$mode" == "unused" ]]; then
            echo "[deploy.sh] ℹ️ finance topology=unused (route=${route}): no proxy_pass references yeoljeong_finance_api; internal and public finance health checks skipped"
        fi
        finance_local_sidecar_list "$mode" || failure="$FINANCE_SIDECAR_ERROR"
    fi

    if [[ -z "$failure" ]]; then
        for sidecar in "${FINANCE_SIDECARS[@]}"; do
            if ! docker inspect "$sidecar" >/dev/null 2>&1; then
                failure="required sidecar absent: ${sidecar}"
                break
            fi
            echo "[deploy.sh] ↻ mounted sidecar restart: ${sidecar}"
            if ! docker restart "$sidecar" >/dev/null 2>&1; then
                failure="mounted sidecar restart failed: ${sidecar}"
                break
            fi
        done
    fi

    if [[ -z "$failure" && "$mode" != "unused" ]]; then
        # Local: internal warmup after the restart. Remote: nothing was
        # restarted, the same probe just verifies the tunnel's real upstream.
        if ! finance_route_health_wait "$internal_url" "$health_wait"; then
            health_elapsed="$health_wait"
            failure="mounted sidecar internal health failed after ${health_elapsed}s: finance ${mode} (${internal_url})"
        fi
    fi

    if [[ -z "$failure" && "$mode" != "unused" ]]; then
        # This route is intentionally authenticated. 401/403 verifies nginx
        # routing plus the auth gate; the internal probe above verifies the
        # sidecar process itself. Only a missing/broken public route fails.
        FINANCE_PUBLIC_CODE=""
        if ! finance_public_route_check "$public_url"; then
            failure="mounted sidecar public health failed: finance ${mode} (${public_url}, http=${FINANCE_PUBLIC_CODE})"
        fi
    fi

    if [[ -n "$failure" ]]; then
        echo "[deploy.sh] ❌ ${failure}"
        deploy_phase_end "mounted_sidecar_reload" "error" "$failure"
        notify "❌ Blue-Green 인증 실패: ${failure}"
        return 1
    fi
    if [[ "$mode" == "unused" ]]; then
        deploy_phase_end "mounted_sidecar_reload" "success" "finance topology=unused: no proxy_pass references yeoljeong_finance_api; finance health checks skipped"
        return 0
    fi
    deploy_phase_end "mounted_sidecar_reload" "success" "finance topology=${mode}: mounted sidecars reloaded/verified and publicly healthy"
}

# Writes the resume-owner FILE marker (a hint for the resume scanner, not the
# DB owner_instance/owner_epoch lease fence, which this never touches) and reads
# it back. rc 1 if the write failed or the stored value differs: a marker that
# was not proven to be written must never be reported as set.
# $1 container $2 true|false
set_resume_owner_marker() {
    local container="$1" value="$2" observed
    [[ "$value" == "true" || "$value" == "false" ]] || return 1
    docker exec "$container" sh -c "printf ${value} > /tmp/aads_execution_resume_owner" >/dev/null 2>&1 || return 1
    observed="$(docker exec "$container" sh -c 'cat /tmp/aads_execution_resume_owner' 2>/dev/null)" || return 1
    [[ "$observed" == "$value" ]]
}

# Prints the routing of the aads_api upstream block only, as sorted
# "active:PORT backup:PORT " tokens (empty when the file/block is unreadable).
# Shared by the stale-rollback guard and the post-edit verification so both
# judge the file with the same parser.
aads_api_routing_snapshot() {
    awk '
        /^[[:space:]]*upstream[[:space:]]+aads_api[[:space:]]*\{/ {in_up=1; next}
        in_up && /^[[:space:]]*\}/ {in_up=0}
        in_up && /^[[:space:]]*server[[:space:]]+127\.0\.0\.1:[0-9]+/ {
            match($0, /127\.0\.0\.1:[0-9]+/)
            port=substr($0, RSTART + 10, RLENGTH - 10)
            print (($0 ~ /[[:space:]]backup[[:space:];]/) ? "backup:" : "active:") port
        }' "$UPSTREAM_CONF" 2>/dev/null | sort | tr '\n' ' '
}

# Called with the nginx lock held, before the rollback edits anything. The
# rollback only makes sense while the release this deploy cut over is still the
# one being routed; if a newer release (or a slot reuse) took over meanwhile,
# routing back to the old slot would undo someone else's release.
# rc 0 = still the expected state; rc 1 + ROLLBACK_STALE_REASON otherwise.
# $1 new_port $2 new_container $3 old_port $4 old_container
rollback_expected_state_ok() {
    local new_port="$1" new_container="$2" old_port="$3" old_container="$4"
    local generation_now routing image image_id tag_id marker_port marker_container
    ROLLBACK_STALE_REASON=""

    generation_now="$(tr -d '[:space:]' < "$DEPLOY_GENERATION_FILE" 2>/dev/null || true)"
    if [[ -z "${DEPLOY_GENERATION:-}" || "$generation_now" != "$DEPLOY_GENERATION" ]]; then
        ROLLBACK_STALE_REASON="deploy generation is no longer this release (expected ${DEPLOY_GENERATION:-unset}, current ${generation_now:-missing}); a newer deploy took over"
        return 1
    fi

    routing="$(aads_api_routing_snapshot)"
    if [[ "$routing" != "active:${new_port} backup:${old_port} " ]]; then
        ROLLBACK_STALE_REASON="nginx aads_api routing is not the cutover state active=${new_port} backup=${old_port} (observed: ${routing:-none})"
        return 1
    fi

    image="$(docker inspect "$new_container" --format '{{.Config.Image}}' 2>/dev/null || true)"
    image_id="$(docker inspect "$new_container" --format '{{.Image}}' 2>/dev/null || true)"
    tag_id="$(docker image inspect "aads-server:${AADS_RELEASE_SHA}" --format '{{.Id}}' 2>/dev/null || true)"
    if [[ -z "${AADS_RELEASE_SHA:-}" || "${image##*:}" != "$AADS_RELEASE_SHA" ]]; then
        ROLLBACK_STALE_REASON="${new_container} runs image ${image:-unknown}, not this release ${AADS_RELEASE_SHA:-unset}"
        return 1
    fi
    if [[ -z "$image_id" || "$image_id" != "$tag_id" ]]; then
        ROLLBACK_STALE_REASON="${new_container} image generation ${image_id:-unknown} differs from aads-server:${AADS_RELEASE_SHA} (${tag_id:-missing})"
        return 1
    fi

    marker_port="$(tr -d '[:space:]' < "$ACTIVE_PORT_FILE" 2>/dev/null || true)"
    marker_container="$(tr -d '[:space:]' < "$ACTIVE_CONTAINER_FILE" 2>/dev/null || true)"
    if [[ "$marker_port" != "$new_port" || "$marker_container" != "$new_container" ]]; then
        ROLLBACK_STALE_REASON="active-slot state is ${marker_container:-missing}:${marker_port:-missing}, expected ${new_container}:${new_port}"
        return 1
    fi
}

# Restores routing to the previous (still healthy, not yet recycled) API slot
# after a post-cutover finance/sidecar failure. Everything that edits the shared
# nginx upstream happens inside one short lock window. The file is edited with
# the same targeted sed as the cutover (not by copying the pre-deploy snapshot)
# so unrelated upstream changes made while the lock was released survive.
# Inside the lock it first proves the cutover state is still current
# (rollback_expected_state_ok) and refuses to roll back a superseded release.
# rc 0 = routing, active-slot state and the resume-owner file markers are back
# on the previous slot, each write was read back, and routed health passed;
# rc 1 = NOT (fully) restored. SIDECAR_ROLLBACK_ERROR says why, including any
# failed compensation step, and the caller must not report success or assume
# routing/state/ownership agree. DB lease fencing (owner_instance/owner_epoch)
# is not touched, so live execution leases are never force-migrated by this.
# Never rebuilds or restarts either slot, so streams on both slots are untouched.
# $1 new_port $2 new_container $3 old_port $4 old_container
rollback_cutover_after_sidecar_failure() {
    local new_port="$1" new_container="$2" old_port="$3" old_container="$4"
    local backup="${UPSTREAM_CONF}.pre_sidecar_rollback" old_slot new_slot
    local routed=false owner_touched=false edited=false comp_failed=""
    local comp_restored=false comp_reloaded=false
    local sed_rc=0 observed_routing=""
    SIDECAR_ROLLBACK_ERROR=""

    deploy_phase_start "sidecar_failure_rollback" "rolling_back"
    if [[ "$old_port" == "${GREEN_PORT:-}" ]]; then
        old_slot="green"; new_slot="blue"
    else
        old_slot="blue"; new_slot="green"
    fi

    if ! curl -sf --max-time 5 "http://127.0.0.1:${old_port}/api/v1/health" >/dev/null 2>&1; then
        SIDECAR_ROLLBACK_ERROR="previous slot ${old_container}:${old_port} is not healthy; routing left on ${new_container}:${new_port}"
    elif ! acquire_nginx_switch_lock; then
        SIDECAR_ROLLBACK_ERROR="nginx upstream lock unavailable; routing left on ${new_container}:${new_port}"
    else
        if ! rollback_expected_state_ok "$new_port" "$new_container" "$old_port" "$old_container"; then
            SIDECAR_ROLLBACK_ERROR="stale rollback refused, routing/state/ownership left untouched: ${ROLLBACK_STALE_REASON}"
        elif ! cp "$UPSTREAM_CONF" "$backup"; then
            SIDECAR_ROLLBACK_ERROR="cannot snapshot ${UPSTREAM_CONF}; routing left on ${new_container}:${new_port}"
        else
            edited=true
            # Scoped to the aads_api block so an unrelated upstream that happens
            # to use the same port is never rewritten. The sed result and the
            # re-parsed routing are both checked: a failed, partial or silently
            # no-op edit must never reach reload/state/owner steps.
            sed -i -E \
                -e "/^[[:space:]]*upstream[[:space:]]+aads_api[[:space:]]*\{/,/^[[:space:]]*\}/{" \
                -e "s/server 127\.0\.0\.1:${old_port} [^;]*;/server 127.0.0.1:${old_port} max_fails=1 fail_timeout=10s;/g" \
                -e "s/server 127\.0\.0\.1:${new_port} [^;]*;/server 127.0.0.1:${new_port} max_fails=1 fail_timeout=10s backup;/g" \
                -e "}" \
                -e "s|# Active API slot:.*|# Active API slot: ${old_slot} (${old_port}). ${new_slot^} (${new_port}) is kept as rollback backup.|" \
                "$UPSTREAM_CONF" || sed_rc=$?
            observed_routing="$(aads_api_routing_snapshot)"
            if (( sed_rc != 0 )); then
                SIDECAR_ROLLBACK_ERROR="upstream edit failed (sed rc=${sed_rc}) while restoring ${old_container}:${old_port}"
            elif [[ "$observed_routing" != "active:${old_port} backup:${new_port} " ]]; then
                SIDECAR_ROLLBACK_ERROR="upstream edit did not produce active=${old_port} backup=${new_port} (observed: ${observed_routing:-none}); nginx not reloaded"
            elif ! nginx_config_test >/dev/null 2>&1; then
                SIDECAR_ROLLBACK_ERROR="nginx config test failed while restoring ${old_container}:${old_port}"
            elif ! nginx_reload; then
                SIDECAR_ROLLBACK_ERROR="nginx reload failed while restoring ${old_container}:${old_port}"
            else
                routed=true
                sleep 2
                if ! write_active_slot_state "$old_port" "$old_container" "deploy.sh" "sidecar failure routing rollback" \
                    || ! verify_container_slot_markers "$old_container" "$old_port" "$old_container"; then
                    SIDECAR_ROLLBACK_ERROR="active-slot state/marker restore failed for ${old_container}:${old_port}"
                else
                    owner_touched=true
                    if ! set_resume_owner_marker "$old_container" true; then
                        SIDECAR_ROLLBACK_ERROR="resume-owner marker write/read-back failed on ${old_container}"
                    elif ! set_resume_owner_marker "$new_container" false; then
                        SIDECAR_ROLLBACK_ERROR="resume-owner marker write/read-back failed on ${new_container}"
                    elif ! curl -sf --max-time 5 "http://127.0.0.1:${old_port}/api/v1/health" >/dev/null 2>&1 \
                        || ! curl -sf --max-time 5 -H "Host: ${DOWNTIME_PROBE_HOST}" "http://127.0.0.1/api/v1/health" >/dev/null 2>&1; then
                        SIDECAR_ROLLBACK_ERROR="routed health failed after restoring ${old_container}:${old_port}"
                    fi
                fi
            fi
            if [[ -n "$SIDECAR_ROLLBACK_ERROR" && "$edited" == "true" ]]; then
                # Put the file, reload, state and markers back as they were so
                # routing and markers agree again. Every compensation step is
                # checked and each one gates the next with an explicit
                # condition (never errexit/`||` chaining, which a caller's
                # `if ! rollback...` silently disables): a reload is only
                # allowed after the restored file is proven to be the snapshot
                # AND to route active=${new_port} backup=${old_port}; state and
                # marker writes are only allowed after that reload succeeded.
                # nginx accepts an all-active upstream, so `nginx -t` passing
                # proves nothing about routing.
                comp_restored=true
                comp_reloaded=false
                if ! cp "$backup" "$UPSTREAM_CONF" 2>/dev/null; then
                    comp_failed+="upstream file restore (cp failed); "
                    comp_restored=false
                fi
                if ! cmp -s "$backup" "$UPSTREAM_CONF"; then
                    comp_failed+="upstream file restore (content differs from snapshot); "
                    comp_restored=false
                fi
                if [[ "$(aads_api_routing_snapshot)" != "active:${new_port} backup:${old_port} " ]]; then
                    comp_failed+="upstream routing re-verification (expected active=${new_port} backup=${old_port}); "
                    comp_restored=false
                fi
                if [[ "$comp_restored" != "true" ]]; then
                    comp_failed+="nginx not reloaded; "
                fi
                if [[ "$comp_restored" == "true" ]]; then
                    if ! nginx_config_test >/dev/null 2>&1; then
                        comp_failed+="nginx config test of restored upstream (not reloaded); "
                    elif ! nginx_reload >/dev/null 2>&1; then
                        comp_failed+="nginx reload of restored upstream; "
                    else
                        comp_reloaded=true
                    fi
                fi
                if [[ "$routed" == "true" ]]; then
                    if [[ "$comp_reloaded" != "true" ]]; then
                        comp_failed+="active-slot state/marker restore skipped (restored routing not proven reloaded, state left as written); "
                    else
                        if ! write_active_slot_state "$new_port" "$new_container" "deploy.sh" "sidecar failure rollback failed; routing kept" >/dev/null 2>&1; then
                            comp_failed+="active-slot state restore; "
                        fi
                        if ! verify_container_slot_markers "$new_container" "$new_port" "$new_container" >/dev/null 2>&1; then
                            comp_failed+="slot marker re-verification; "
                        fi
                    fi
                fi
                if [[ "$owner_touched" == "true" ]]; then
                    if [[ "$comp_reloaded" != "true" ]]; then
                        comp_failed+="resume-owner marker restore skipped (restored routing not proven reloaded, markers left as written); "
                    else
                        if ! set_resume_owner_marker "$new_container" true; then
                            comp_failed+="resume-owner marker restore on ${new_container}; "
                        fi
                        if ! set_resume_owner_marker "$old_container" false; then
                            comp_failed+="resume-owner marker restore on ${old_container}; "
                        fi
                    fi
                fi
                if [[ -n "$comp_failed" ]]; then
                    SIDECAR_ROLLBACK_ERROR="${SIDECAR_ROLLBACK_ERROR}; COMPENSATION FAILED (${comp_failed%; }): routing/state/ownership may disagree, manual verification required"
                fi
            fi
        fi
        release_nginx_switch_lock
    fi

    if [[ -n "$SIDECAR_ROLLBACK_ERROR" ]]; then
        echo "[deploy.sh] ❌ sidecar failure routing rollback FAILED: ${SIDECAR_ROLLBACK_ERROR}"
        audit_control "sidecar-failure-rollback" "${new_container}:${new_port}->${old_container}:${old_port}" "failed" "$SIDECAR_ROLLBACK_ERROR"
        deploy_phase_end "sidecar_failure_rollback" "failed" "$SIDECAR_ROLLBACK_ERROR"
        return 1
    fi
    audit_control "sidecar-failure-rollback" "${new_container}:${new_port}->${old_container}:${old_port}" "success" "routing, active-slot state and resume-owner file markers restored and read back; routed health passed (DB lease fencing untouched)"
    deploy_phase_end "sidecar_failure_rollback" "success" "routing restored to ${old_container}:${old_port}"
}

schedule_standby_sync_retry() {
    local run_id="${1:-}"
    local sync_script="${STATE_DIR}/scripts/sync-standby.sh"
    local unit="aads-standby-sync-${run_id:-unknown}"
    [[ "$run_id" =~ ^[0-9]+$ ]] || return 1
    [[ -x "$sync_script" ]] || {
        echo "[deploy.sh] ⚠️ standby retry script missing: ${sync_script}"
        return 1
    }
    # The retry must be detached.  Without --no-block systemd-run waits for the
    # oneshot to finish while this deploy still owns aads-deploy.flock; the
    # retry then waits on that same lock and deadlocks the whole release queue.
    if systemd-run --no-block --unit="$unit" --collect --property=Type=oneshot \
        /bin/bash -lc "set +e; for _attempt in \$(seq 1 120); do '${sync_script}' --deploy-run-id '${run_id}'; _rc=\$?; [[ \$_rc -eq 0 ]] && exit 0; [[ \$_rc -eq 2 || \$_rc -eq 3 ]] || exit \$_rc; sleep 30; done; exit 3" \
        >/dev/null 2>&1; then
        echo "[deploy.sh] standby 동기화 재시도 예약: unit=${unit}, run=${run_id}"
        return 0
    fi
    echo "[deploy.sh] ⚠️ standby 동기화 재시도 예약 실패: unit=${unit}"
    return 1
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

# 최선 노력: 후보 슬롯을 쥔 채팅 세션 id(앞 8자, 최대 5건). 실패해도 배포를 막지 않는다.
target_active_session_ids() {
    local container
    container="$(container_for_port "$1")"
    [[ -n "$container" ]] || return 0
    docker exec aads-postgres psql -U aads -d aads -Atc "
        SELECT COALESCE(string_agg(left(session_id::text, 8), ','), '')
        FROM (
            SELECT session_id FROM chat_turn_executions
            WHERE status IN ('running','retrying')
              AND completed_at IS NULL
              AND owner_instance = '${container}'
            LIMIT 5
        ) s;
    " 2>/dev/null | tr -d '[:space:]' || true
}

# 후보 슬롯 drain 게이트. 반환: 0=진행, 1=차단(호출자가 exit 1).
# 2026-10-06 CEO 지시: 180초 대기 후에도 스트림이 남으면 차단하지 않고 진행한다.
# #5603~#5607 이 'active streams=1'(대부분 CEO 채팅 자신)로 41분간 막혔다.
# AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT 대기는 그대로, 시간 초과 후 동작만
# AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=proceed(기본)|block 으로 고른다.
# AADS_DEPLOY_ALLOW_BUSY_TARGET=true 는 기존대로 대기 없이 즉시 진행.
# 헬스체크·이미지 검증·롤백 게이트는 이 함수 밖이며 완화되지 않는다.
target_slot_drain_gate() {
    local action="${AADS_DEPLOY_DRAIN_TIMEOUT_ACTION:-proceed}"
    local sessions note
    TARGET_DRAIN_PROCEED_NOTE=""
    case "$action" in
        proceed|block) ;;
        *) action="proceed" ;;
    esac
    TARGET_STREAMS="$(stream_count_for_port "$NEW_PORT")"
    local_target_drain_max="${AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT:-180}"
    local_target_drain_interval="${AADS_DEPLOY_TARGET_DRAIN_POLL_SECONDS:-10}"
    local_target_elapsed=$(($(date +%s) - TARGET_DRAIN_STARTED_EPOCH))
    if [[ ! "$local_target_drain_max" =~ ^[0-9]+$ ]]; then
        local_target_drain_max="180"
    fi
    if [[ ! "$local_target_drain_interval" =~ ^[0-9]+$ ]] || [[ "$local_target_drain_interval" -lt 5 ]]; then
        local_target_drain_interval="10"
    fi
    if [[ "$TARGET_STREAMS" =~ ^[0-9]+$ ]] && [[ "$TARGET_STREAMS" -gt 0 ]] && [[ "${AADS_DEPLOY_ALLOW_BUSY_TARGET:-false}" != "true" ]]; then
        echo "[deploy.sh] ⏳ 전환 대상 ${NEW_CONTAINER}:${NEW_PORT} 활성 스트림 ${TARGET_STREAMS}건 — build와 겹친 drain 창 확인 (${local_target_elapsed}/${local_target_drain_max}초)"
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
            sessions="$(target_active_session_ids "$NEW_PORT")"
            if [[ "$action" == "block" ]]; then
                echo "[deploy.sh] ❌ 전환 대상 ${NEW_CONTAINER}:${NEW_PORT}에 활성 스트림 ${TARGET_STREAMS}건 잔존 — 정책 block 으로 배포 차단"
                echo "[deploy.sh]    기본 정책은 proceed 입니다. AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=block 이 명시된 경우에만 차단합니다."
                notify "❌ Blue-Green 중단: target slot ${NEW_CONTAINER}:${NEW_PORT} active streams=${TARGET_STREAMS}"
                deploy_phase_end "target_slot_drain" "blocked" "target slot ${NEW_CONTAINER}:${NEW_PORT} active streams=${TARGET_STREAMS}"
                record_deploy "blocked" "$MODE" "target slot ${NEW_CONTAINER}:${NEW_PORT} active streams=${TARGET_STREAMS}"
                return 1
            fi
            echo "[deploy.sh] ⚠️ drain timeout — active=${TARGET_STREAMS} 잔존, 정책 proceed 로 전환 진행 (sessions=${sessions:-unknown}, waited=${local_target_elapsed}s)"
            note="target_drain proceeded_busy: active=${TARGET_STREAMS} sessions=${sessions:-unknown} waited=${local_target_elapsed}s policy=proceed"
            TARGET_DRAIN_PROCEED_NOTE="$note"
            audit_control "target-drain" "${NEW_CONTAINER}:${NEW_PORT}" "proceeded_busy" \
                "active=${TARGET_STREAMS}; sessions=${sessions:-unknown}; waited=${local_target_elapsed}s; policy=proceed"
            notify "⚠️ target slot ${NEW_CONTAINER}:${NEW_PORT} 활성 스트림 ${TARGET_STREAMS}건 잔존 — drain timeout 후 배포 진행"
        fi
    elif [[ "$TARGET_STREAMS" != "0" ]]; then
        echo "[deploy.sh] ⚠️ 전환 대상 ${NEW_CONTAINER}:${NEW_PORT} active-streams 확인값=${TARGET_STREAMS} — 미기동/미응답 슬롯으로 판단하고 재빌드를 진행합니다."
    fi
    return 0
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

# 양 슬롯이 같은 이미지 digest 를 실제로 돌리는지 docker 로 직접 확인한다.
# 어느 한쪽이라도 읽지 못하면 불일치로 본다(fail closed).
standby_same_digest_verified() {
    local active_container="${1:-}"
    local standby_container="${2:-}"
    STANDBY_VERIFY_ACTIVE_IMAGE=""
    STANDBY_VERIFY_STANDBY_IMAGE=""
    [[ -n "$active_container" && -n "$standby_container" ]] || return 1
    STANDBY_VERIFY_ACTIVE_IMAGE="$(docker inspect "$active_container" --format '{{.Image}}' 2>/dev/null || true)"
    STANDBY_VERIFY_STANDBY_IMAGE="$(docker inspect "$standby_container" --format '{{.Image}}' 2>/dev/null || true)"
    [[ -n "$STANDBY_VERIFY_ACTIVE_IMAGE" && -n "$STANDBY_VERIFY_STANDBY_IMAGE" ]] || return 1
    [[ "$STANDBY_VERIFY_ACTIVE_IMAGE" == "$STANDBY_VERIFY_STANDBY_IMAGE" ]]
}

# sync_standby_slot_after_drain 의 종료코드를 릴리스 인증 상태로 번역한다.
#   0 = 동기화·검증 완료. 단, 호출자가 digest 를 독립적으로 다시 확인해야만 인증.
#   2 = 대기 슬롯에 활성 스트림이 남아 drain 미완료 (pending)
#   3 = 동기화를 건너뜀: lock busy / stale generation / ownership 변경 (skipped)
#   그 외 = 실패
# 반환: 0 = 배포 계속(인증 보류 가능), 1 = 치명 실패(호출자가 exit 1).
# STANDBY_SYNC_DEFERRED=true 이면 최종 상태는 success_partial 이다.
handle_standby_sync_result() {
    local rc="${1:-1}"
    STANDBY_SYNC_DEFERRED=false
    STANDBY_SYNC_DEFER_REASON=""
    case "$rc" in
        0)
            if standby_same_digest_verified "${NEW_CONTAINER:-}" "${OLD_CONTAINER:-}"; then
                deploy_phase_end "standby_same_digest_sync" "success" ""
                return 0
            fi
            notify "❌ Blue-Green 인증 실패: standby same-digest 동기화 실패"
            deploy_phase_end "standby_same_digest_sync" "failed" "standby same-digest sync failed for ${OLD_CONTAINER}:${OLD_PORT}: digest not verified after sync"
            record_deploy "failed" "$MODE" "standby same-digest sync failed for ${OLD_CONTAINER}:${OLD_PORT}: digest not verified after sync"
            return 1
            ;;
        2)
            # 활성 스트림이 남아 드레인하지 못한 경우. 진행 중인 대화를 끊지
            # 않으려는 의도된 동작이며 서비스에는 영향이 없다. 배포를 여기서
            # 중단하지 않고 검증을 계속한 뒤 success_partial 로 닫는다.
            STANDBY_SYNC_DEFERRED=true
            STANDBY_SYNC_DEFER_REASON="active streams"
            notify "⚠️ standby 동기화 보류: 활성 스트림 때문에 ${OLD_CONTAINER} 가 구버전으로 남음"
            deploy_phase_end "standby_same_digest_sync" "skipped" "deferred: active streams on ${OLD_CONTAINER}:${OLD_PORT}"
            schedule_standby_sync_retry "$DEPLOY_RUN_ID" || true
            echo "[deploy.sh] ⚠️ standby 동기화 보류 — 활성 슬롯은 새 릴리스로 정상 동작"
            return 0
            ;;
        3)
            STANDBY_SYNC_DEFERRED=true
            STANDBY_SYNC_DEFER_REASON="skipped: ${STANDBY_SYNC_SKIP_REASON:-unknown}"
            notify "⚠️ standby 동기화 건너뜀(${STANDBY_SYNC_SKIP_REASON:-unknown}): ${OLD_CONTAINER} 가 같은 digest 로 인증되지 않음"
            deploy_phase_end "standby_same_digest_sync" "skipped" "skipped: ${STANDBY_SYNC_SKIP_REASON:-unknown} on ${OLD_CONTAINER}:${OLD_PORT}; same-digest not verified"
            schedule_standby_sync_retry "$DEPLOY_RUN_ID" || true
            echo "[deploy.sh] ⚠️ standby 동기화 건너뜀 — 릴리스 인증 보류 (${STANDBY_SYNC_SKIP_REASON:-unknown})"
            return 0
            ;;
        *)
            notify "❌ Blue-Green 인증 실패: standby same-digest 동기화 실패"
            deploy_phase_end "standby_same_digest_sync" "failed" "standby same-digest sync failed for ${OLD_CONTAINER}:${OLD_PORT}"
            record_deploy "failed" "$MODE" "standby same-digest sync failed for ${OLD_CONTAINER}:${OLD_PORT}"
            return 1
            ;;
    esac
}

# 최종 상태 결정. 인증(success)은 standby 가 보류되지 않았고, bluegreen 이면
# 5분 모니터링 뒤에도 양 슬롯 digest 가 같을 때만 허용한다.
resolve_final_deploy_status() {
    FINAL_DEPLOY_STATUS="success"
    FINAL_DEPLOY_ERROR=""
    if [[ "${STANDBY_SYNC_DEFERRED:-false}" == "true" ]]; then
        FINAL_DEPLOY_STATUS="success_partial"
        FINAL_DEPLOY_ERROR="standby sync deferred: ${STANDBY_SYNC_DEFER_REASON:-active streams}"
    elif [[ "${MODE:-}" == "bluegreen" ]] \
            && ! standby_same_digest_verified "${NEW_CONTAINER:-}" "${OLD_CONTAINER:-}"; then
        FINAL_DEPLOY_STATUS="success_partial"
        FINAL_DEPLOY_ERROR="standby sync deferred: active/standby image digest mismatch at final certification"
    fi
    # 성공으로 끝나도 drain timeout 후 busy 슬롯으로 진행했다는 사실은 원장에 남긴다.
    if [[ -n "${TARGET_DRAIN_PROCEED_NOTE:-}" ]]; then
        FINAL_DEPLOY_ERROR="${FINAL_DEPLOY_ERROR:+${FINAL_DEPLOY_ERROR}; }${TARGET_DRAIN_PROCEED_NOTE}"
    fi
    if [[ -n "${P0P1_NOISE_NOTE:-}" ]]; then
        FINAL_DEPLOY_ERROR="${FINAL_DEPLOY_ERROR:+${FINAL_DEPLOY_ERROR}; }${P0P1_NOISE_NOTE}"
    fi
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
        local min_wait="${AADS_DEPLOY_STANDBY_SYNC_MIN_WAIT:-0}"
        if [[ "$min_wait" != "0" ]]; then
            echo "[deploy.sh] standby sync grace wait ${old_container}:${old_port} ${min_wait}s"
            sleep "$min_wait"
        fi

        exec 9>"/tmp/aads-standby-sync.lock"
        # return 3 = skipped. 동기화를 하지 않았으므로 same-digest 인증은 성립하지
        # 않는다. 예전에는 return 0 이라 호출자가 "성공"으로 읽었다(SHA 520b5326).
        flock -w 30 9 || {
            STANDBY_SYNC_SKIP_REASON="standby lock busy"
            audit_control "standby-sync" "${old_container}:${old_port}" "skipped" "$STANDBY_SYNC_SKIP_REASON"
            return 3
        }
        if ! standby_ownership_valid "$old_container" "$old_port" "$expected_generation"; then
            STANDBY_SYNC_SKIP_REASON="stale generation or slot became active"
            audit_control "standby-sync" "${old_container}:${old_port}" "skipped" "$STANDBY_SYNC_SKIP_REASON"
            return 3
        fi
        reconcile_inactive_target_recovery_executions "$old_container"
        echo "[deploy.sh] standby sync pre-drain graceful shutdown trigger on inactive slot :${old_port}"
        curl -sf -X POST "http://127.0.0.1:${old_port}/api/v1/pc-agent/graceful-shutdown" \
            -H "Content-Type: application/json" 2>/dev/null || true

        # 기본 inline 대기는 0초다. old slot이 busy이면 즉시 success_partial로
        # 물러나고, deploy flock이 풀린 뒤 별도 sync-standby worker가 30초
        # 간격으로 재시도한다. 필수 5분 P0/P1 관측과 drain 시간을 겹치므로
        # 동일한 stream 보호·same-digest 인증을 직렬 180초 대기 없이 달성한다.
        #
        # 이 대기는 타임아웃돼도 스트림을 끊지 않는다 — BLOCKED 로 물러나
        # 대기 슬롯을 구버전 그대로 둘 뿐이다(아래 return 2). 그러니 길게
        # 기다려서 얻는 것은 "운 좋게 스트림이 0이 되는 경우" 하나뿐인데,
        # 채팅이 상시 도는 이 시스템에서는 그 순간이 거의 오지 않는다.
        #
        # 실측 #4719: 600초를 다 기다리고도 `active streams=1` 로 BLOCKED.
        # 그 10분 동안 배포 락을 잡아 다음 배포 #4721 이 통째로 밀렸다.
        # 어차피 물러날 것이라면 빨리 물러나 락을 놓는 편이 낫다.
        local drain_max="${AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT:-0}"
        local drain_interval="${AADS_DEPLOY_STANDBY_SYNC_POLL_SECONDS:-5}"
        local elapsed=0
        local active
        local zero_seen=0
        if [[ ! "$drain_max" =~ ^[0-9]+$ ]]; then
            drain_max="600"
        fi
        if [[ ! "$drain_interval" =~ ^[0-9]+$ ]] || [[ "$drain_interval" -lt 5 ]]; then
            drain_interval="5"
        fi
        active="$(stream_count_for_port "$old_port")"
        while [[ $elapsed -lt $drain_max ]]; do
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
            active="$(stream_count_for_port "$old_port")"
        done

        if [[ "${active:-0}" != "0" && -n "${active:-}" ]]; then
            # The standby slot no longer receives new nginx traffic, but it still owns
            # live CEO chat turns. Preserve those turns and fail the release
            # certification closed: same-digest standby sync is mandatory and must
            # never be reported as complete while the old release is still serving.
            set_deploy_stream_phase_metadata "$old_container" "$old_port" "${active:-unknown}" "$elapsed" "$drain_max"
            echo "[deploy.sh] standby sync BLOCKED: ${old_container}:${old_port} still has active streams=${active}; release not certified"
            audit_control "standby-sync" "${old_container}:${old_port}" "blocked" "drain timeout active=${active}; same-digest certification withheld"
            # 진짜 동기화 실패와 구분한다. 여기까지 왔다는 것은 컷오버가 이미
            # 성공해 새 릴리스가 트래픽을 받고 있다는 뜻이고, 남은 것은 대기
            # 슬롯이 구버전이라는 사실뿐이다. 이를 배포 전체 실패로 기록하면
            # 성공률이 실제보다 낮게 보이고 자가치유가 헛된 재시도를 건다
            # (2026-09-13: 이 사유로 3건이 failed 로 집계됐다).
            return 2
        fi
        set_deploy_stream_phase_metadata "$old_container" "$old_port" "${active:-0}" "$elapsed" "$drain_max"

        if ! standby_ownership_valid "$old_container" "$old_port" "$expected_generation"; then
            # RC7: ownership change after drain is not a release failure — the slot was
            # re-activated by a newer deploy generation. 실패로 기록하지는 않지만
            # 이 릴리스의 standby 는 동기화되지 않았으므로 인증도 하지 않는다.
            STANDBY_SYNC_SKIP_REASON="ownership changed after drain"
            audit_control "standby-sync" "${old_container}:${old_port}" "skipped" "$STANDBY_SYNC_SKIP_REASON"
            return 3
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
            if ! standby_same_digest_verified "$active_container" "$old_container"; then
                echo "[deploy.sh] standby sync ERROR: image digest mismatch active=${STANDBY_VERIFY_ACTIVE_IMAGE:-missing} standby=${STANDBY_VERIFY_STANDBY_IMAGE:-missing}"
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

        # ①-1 이미지 빌드 — target drain 관측과 겹쳐 실행한다.
        #
        # 빌드는 이미지를 만들 뿐 실행 중인 컨테이너를 건드리지 않는다. 그런데
        # 기존 순서는 활성 스트림이 빠지기를 최대 1800초 기다린 뒤에야 빌드를
        # 시작했다. 2026-09-13 배포 #402 실측 — 58분 중 30분이 순수 대기였고
        # 그 뒤에야 7분짜리 빌드가 돌았다.
        #
        # 순서를 뒤집으면 빌드하는 동안 스트림이 자연히 빠져 추가 대기가
        # 대부분 사라진다. 컨테이너 교체(①-2)는 drain 이후로 그대로 둔다 —
        # 그때가 실제로 스트림을 끊는 시점이다.
        # inactive target의 drain deadline은 빌드가 시작될 때부터 흐른다.
        # 예전에는 10~20분 빌드가 끝난 뒤 다시 최대 180초를 기다려, 이미
        # 충분히 drain된 시간까지 직렬로 더했다. 빌드는 컨테이너를 건드리지
        # 않으므로 이 관측 창을 겹쳐도 실행 중 스트림 보호 계약은 그대로다.
        # 필수 sidecar 토폴로지는 빌드·drain·cutover 전에 닫는다 (#5814: cutover 뒤에야 발견).
        if ! preflight_mounted_sidecar_topology; then
            record_deploy "blocked" "$MODE" "sidecar topology preflight failed before build"
            exit 1
        fi

        TARGET_DRAIN_STARTED_EPOCH="$(date +%s)"
        reconcile_inactive_target_recovery_executions "$NEW_CONTAINER"
        TARGET_STREAMS_BEFORE_BUILD="$(stream_count_for_port "$NEW_PORT")"
        echo "[deploy.sh] target drain window started before build: ${NEW_CONTAINER}:${NEW_PORT} active=${TARGET_STREAMS_BEFORE_BUILD:-unknown}"
        audit_control "target-drain-window" "${NEW_CONTAINER}:${NEW_PORT}" "started" \
            "active_before_build=${TARGET_STREAMS_BEFORE_BUILD:-unknown}; overlaps=build_candidate_image"

        # 파괴적 SQL 은 빌드 전에 거른다 — 10분 빌드 뒤에 막히면 헛빌드다.
        apply_release_schema_migrations "schema_migration_plan" --plan

        cd "$COMPOSE_DIR"
        deploy_phase_start "build_candidate_image" "running"
        echo "[deploy.sh] ① release image 1회 빌드 (${AADS_RELEASE_SHA})..."
        build_release_image
        deploy_phase_end "build_candidate_image" "success" "image built; container start pending drain"

        deploy_phase_start "target_slot_drain" "running"
        reconcile_inactive_target_recovery_executions "$NEW_CONTAINER"
        if ! target_slot_drain_gate; then
            exit 1
        fi
        set_deploy_stream_phase_metadata "$NEW_CONTAINER" "$NEW_PORT" "${TARGET_STREAMS:-unknown}" "${local_target_elapsed:-0}" "${local_target_drain_max:-0}"
        deploy_phase_end "target_slot_drain" "success" "active_streams=${TARGET_STREAMS}${TARGET_DRAIN_PROCEED_NOTE:+; ${TARGET_DRAIN_PROCEED_NOTE}}"

        # ①-2a 릴리스의 migrations/ 를 ledger 기준으로 전량 적용 — 새 슬롯 기동 전.
        # 실패하면 여기서 끝나고 기존 슬롯이 계속 트래픽을 받는다(additive 전제).
        apply_release_schema_migrations "schema_migrations"

        # ①-2 새 컨테이너 시작 — 이미지는 drain 이전에 이미 만들어 뒀다(①-1).
        cd "$COMPOSE_DIR"
        deploy_phase_start "start_candidate_container" "running"
        echo "[deploy.sh] ① ${NEW_CONTAINER} --no-build 시작..."
        docker compose "${COMPOSE_ENV_ARGS[@]}" $COMPOSE_FILE $PROFILE_CMD up -d --no-build --no-deps --force-recreate "$NEW_CONTAINER"
        if ! verify_container_memory_limit "$NEW_CONTAINER"; then
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            docker rm "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: ${NEW_CONTAINER} memory limit mismatch"
            deploy_phase_end "start_candidate_container" "failed" "${NEW_CONTAINER} memory limit mismatch"
            record_deploy "failed" "$MODE" "${NEW_CONTAINER} memory limit mismatch"
            exit 1
        fi
        deploy_phase_end "start_candidate_container" "success" ""

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
        # The SDK bundle can lag behind the model contract. Check the actual
        # candidate container before routing chat traffic to it; source-level
        # pins alone cannot catch a release built from a divergent branch.
        CANDIDATE_CLAUDE_VERSION="$(docker exec "$NEW_CONTAINER" /usr/local/bin/claude-aads --version 2>/dev/null || true)"
        if ! python3 -c 'import re, sys; m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+) \(Claude Code\)", sys.argv[1]); sys.exit(0 if m and tuple(map(int, m.groups())) >= (2, 1, 280) else 1)' "$CANDIDATE_CLAUDE_VERSION"; then
            echo "[deploy.sh] ❌ ${NEW_CONTAINER} Claude CLI 호환성 실패: ${CANDIDATE_CLAUDE_VERSION:-missing} — 롤백"
            docker stop "$NEW_CONTAINER" 2>/dev/null || true
            docker rm "$NEW_CONTAINER" 2>/dev/null || true
            notify "❌ Blue-Green 실패: ${NEW_CONTAINER} Claude CLI 2.1.280+ 없음"
            deploy_phase_end "candidate_health" "failed" "${NEW_CONTAINER} incompatible Claude CLI: ${CANDIDATE_CLAUDE_VERSION:-missing}"
            record_deploy "failed" "$MODE" "${NEW_CONTAINER} incompatible Claude CLI: ${CANDIDATE_CLAUDE_VERSION:-missing}"
            exit 1
        fi
        echo "[deploy.sh] ✅ ${NEW_CONTAINER} Claude CLI 호환성 확인: ${CANDIDATE_CLAUDE_VERSION}"
        deploy_phase_end "candidate_health" "success" "elapsed=${BG_ELAPSED}s"

        # 전환 전 현재 슬롯은 계속 신규 트래픽을 받으므로 여기서 기다려도
        # busy 서비스에서는 0이 되지 않는다(최근 48시간 p50 68초). 기본은
        # 소유 lease를 스냅샷만 남기고 즉시 graceful nginx cutover한다. 기존
        # nginx worker가 live stream을 유지하고, old slot 재생성은 아래
        # standby gate가 owner_instance/lease를 다시 확인한 뒤에만 수행한다.
        deploy_phase_start "active_slot_drain" "running"
        PRE_CUTOVER_DRAIN_MAX_WAIT="${AADS_DEPLOY_PRE_CUTOVER_DRAIN_MAX_WAIT:-0}"
        PRE_CUTOVER_DRAIN_INTERVAL="${AADS_DEPLOY_PRE_CUTOVER_DRAIN_POLL_SECONDS:-5}"
        [[ "$PRE_CUTOVER_DRAIN_MAX_WAIT" =~ ^[0-9]+$ ]] || PRE_CUTOVER_DRAIN_MAX_WAIT=0
        if [[ ! "$PRE_CUTOVER_DRAIN_INTERVAL" =~ ^[0-9]+$ ]] || [[ "$PRE_CUTOVER_DRAIN_INTERVAL" -lt 1 ]]; then
            PRE_CUTOVER_DRAIN_INTERVAL=5
        fi
        DRAIN_ELAPSED=0
        ACTIVE_STREAMS="$(stream_count_for_port "$CURRENT_PORT")"
        if [[ "$ACTIVE_STREAMS" =~ ^[0-9]+$ ]] && [[ "$ACTIVE_STREAMS" -gt 0 ]] \
                && [[ "$PRE_CUTOVER_DRAIN_MAX_WAIT" -gt 0 ]]; then
            echo "[deploy.sh] ⏳ 현재 슬롯 :${CURRENT_PORT} 활성 스트림 ${ACTIVE_STREAMS}건 — 최대 ${PRE_CUTOVER_DRAIN_MAX_WAIT}초 대기"
            while [[ $DRAIN_ELAPSED -lt "$PRE_CUTOVER_DRAIN_MAX_WAIT" ]]; do
                sleep "$PRE_CUTOVER_DRAIN_INTERVAL"
                DRAIN_ELAPSED=$((DRAIN_ELAPSED + PRE_CUTOVER_DRAIN_INTERVAL))
                ACTIVE_STREAMS="$(stream_count_for_port "$CURRENT_PORT")"
                if [[ "$ACTIVE_STREAMS" == "0" || -z "$ACTIVE_STREAMS" ]]; then
                    echo "[deploy.sh] ✅ 활성 스트림 0건 — 전환 진행 (${DRAIN_ELAPSED}초 대기)"
                    break
                fi
                echo "[deploy.sh]   대기중... active=${ACTIVE_STREAMS} (${DRAIN_ELAPSED}/${PRE_CUTOVER_DRAIN_MAX_WAIT}초)"
            done
            if [[ "$ACTIVE_STREAMS" =~ ^[0-9]+$ ]] && [[ "$ACTIVE_STREAMS" -gt 0 ]]; then
                echo "[deploy.sh] ⚠️ ${ACTIVE_STREAMS}건 스트림 아직 활성 — nginx graceful reload로 전환 진행 (기존 worker가 스트림 유지)"
            fi
        elif [[ "$ACTIVE_STREAMS" =~ ^[0-9]+$ ]] && [[ "$ACTIVE_STREAMS" -gt 0 ]]; then
            echo "[deploy.sh] active lease snapshot=${ACTIVE_STREAMS}; pre-cutover wait disabled — graceful worker drain으로 보호"
        fi
        set_deploy_stream_phase_metadata "$ACTIVE_CONTAINER" "$CURRENT_PORT" "${ACTIVE_STREAMS:-unknown}" "${DRAIN_ELAPSED:-0}" "$PRE_CUTOVER_DRAIN_MAX_WAIT"
        deploy_phase_end "active_slot_drain" "success" "active_streams=${ACTIVE_STREAMS:-unknown}"

        # ③ upstream 전환 (aads-upstream.conf에서 backup 키워드 조작)
        deploy_phase_start "nginx_cutover" "verifying"
        echo "[deploy.sh] ③ upstream 전환: :${CURRENT_PORT} → :${NEW_PORT}"
        acquire_nginx_switch_lock
        cp "$UPSTREAM_CONF" "${UPSTREAM_CONF}.pre_deploy"
        # 새 포트에서 backup 제거, 기존 포트에 backup 추가
        #
        # 주석도 같이 고친다. 2026-09-14 이전에는 `server` 줄만 바꾸고 바로
        # 위의 "Active API slot: blue (8100)" 주석은 그대로 뒀다. 그래서 green
        # 으로 넘어간 뒤에도 파일은 blue 가 활성이라고 적혀 있었고, 상태를
        # 확인하러 온 사람이 그 줄을 읽고 반대로 판단했다. 틀린 주석은 없는
        # 주석보다 나쁘다.
        if [[ "$NEW_PORT" == "$GREEN_PORT" ]]; then
            NEW_SLOT_NAME="green"; OLD_SLOT_NAME="blue"
        else
            NEW_SLOT_NAME="blue"; OLD_SLOT_NAME="green"
        fi
        sed -i -E \
            -e "s/server 127\.0\.0\.1:${NEW_PORT} [^;]*;/server 127.0.0.1:${NEW_PORT} max_fails=1 fail_timeout=10s;/g" \
            -e "s/server 127\.0\.0\.1:${CURRENT_PORT} [^;]*;/server 127.0.0.1:${CURRENT_PORT} max_fails=1 fail_timeout=10s backup;/g" \
            -e "s|# Active API slot:.*|# Active API slot: ${NEW_SLOT_NAME} (${NEW_PORT}). ${OLD_SLOT_NAME^} (${CURRENT_PORT}) is kept as rollback backup.|" \
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
            P0P1_CUTOVER_EPOCH="$(date +%s)"
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

        # The routing transaction is complete. Release the nginx lock before
        # waiting for a separately mounted sidecar to restart; the global
        # release contract allows the lock only through immediate routed health.
        release_nginx_switch_lock

        # The route is live now, but the bind-mounted finance sidecar still
        # serves its pre-release files until it has been restarted and checked.
        if ! reload_mounted_sidecars; then
            if rollback_cutover_after_sidecar_failure "$NEW_PORT" "$NEW_CONTAINER" "$OLD_PORT" "$OLD_CONTAINER"; then
                DEPLOY_UPSTREAM_SWITCHED=false
                notify "❌ Blue-Green 실패: finance sidecar 검증 실패 — ${OLD_CONTAINER}:${OLD_PORT} 로 라우팅 복원 완료 (릴리스 미적용)"
                record_deploy "failed" "$MODE" "mounted sidecar reload failed after nginx cutover; routing rolled back to ${OLD_CONTAINER}:${OLD_PORT}"
            else
                notify "🚨 Blue-Green 실패: finance sidecar 검증 실패 + 라우팅 복원 실패 — 라우팅/상태/소유 일치 미보장, 수동 점검 필요: ${SIDECAR_ROLLBACK_ERROR:0:300}"
                record_deploy "failed" "$MODE" "mounted sidecar reload failed after nginx cutover; routing rollback FAILED (${SIDECAR_ROLLBACK_ERROR}); routing/state/ownership consistency not guaranteed, last cutover target=${NEW_CONTAINER}:${NEW_PORT}"
            fi
            exit 1
        fi

        # Phase 7 기준선: ⑤ 가 이 컨테이너를 다시 만들면 로그가 사라지므로 그 전에 수집한다.
        p0p1_capture_baseline "$OLD_CONTAINER" "${P0P1_CUTOVER_EPOCH:-$(date +%s)}" \
            || echo "[deploy.sh] ⚠️ P0/P1 기준선 수집 실패 — 기준선 없이 감시 (모든 이벤트 신규로 판정)"

        # ⑤ 이전 컨테이너를 drain 후 같은 release로 재빌드해 warm standby로 동기화
        deploy_phase_start "standby_same_digest_sync" "syncing_standby"
        echo "[deploy.sh] ⑤ ${OLD_CONTAINER} standby 동기화"
        write_active_slot_state "$NEW_PORT" "$NEW_CONTAINER" "deploy.sh" "bluegreen routed health passed"
        docker exec "$NEW_CONTAINER" sh -c 'printf true > /tmp/aads_execution_resume_owner' 2>/dev/null || true
        docker exec "$OLD_CONTAINER" sh -c 'printf false > /tmp/aads_execution_resume_owner' 2>/dev/null || true
        _standby_rc=0
        sync_standby_slot_after_drain "$OLD_CONTAINER" "$OLD_PORT" "$DEPLOY_GENERATION" || _standby_rc=$?
        handle_standby_sync_result "$_standby_rc" || exit 1

        HEALTH_URL="http://localhost:${NEW_PORT}/api/v1/health"
        echo "[deploy.sh] ✅ Blue-Green active 전환 + standby same-digest 동기화 완료: :${NEW_PORT} 활성"
        notify "✅ Blue-Green active 전환 완료: :${CURRENT_PORT} → :${NEW_PORT}"
        ;;
    *)
        echo "[deploy.sh] ERROR: 알 수 없는 모드 '$MODE'. bluegreen|warm-deps|code|reload|build 사용"
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
# 릴리스 계약상 P0/P1 관측은 실제 5분보다 짧아질 수 없다. 환경변수도 이
# 하한을 우회하지 못하게 clamp 한다. 더 긴 soak 만 허용한다.
MONITOR_MIN_SECONDS="${AADS_DEPLOY_P0P1_MIN_SECONDS:-300}"
if (( MONITOR_SECONDS < 300 )); then
    MONITOR_SECONDS=300
fi
if (( MONITOR_MIN_SECONDS < 300 )); then
    MONITOR_MIN_SECONDS=300
fi
if (( MONITOR_MIN_SECONDS > MONITOR_SECONDS )); then
    MONITOR_SECONDS="$MONITOR_MIN_SECONDS"
fi
# bluegreen 에서 ACTIVE_CONTAINER 는 컷오버 전 값(= 이전 슬롯)으로 남는다. 감시 대상은
# 트래픽을 받는 신규 슬롯이어야 하고, 이전 슬롯은 ⑤ 에서 standby 로 다시 만들어진다.
MONITOR_CONTAINER="$ACTIVE_CONTAINER"
if [[ "$MODE" == "bluegreen" && -n "${NEW_CONTAINER:-}" ]]; then
    MONITOR_CONTAINER="$NEW_CONTAINER"
fi
MONITOR_SINCE="$(date --iso-8601=seconds)"
MONITOR_SINCE_EPOCH="$(date +%s)"
MONITOR_ELAPSED=0
echo "[deploy.sh] Phase 7: P0/P1 모니터링 (${MONITOR_SECONDS}초, since=${MONITOR_SINCE})..."
while [[ "$MONITOR_ELAPSED" -lt "$MONITOR_SECONDS" ]]; do
    sleep "$MONITOR_INTERVAL"
    MONITOR_ELAPSED=$((MONITOR_ELAPSED + MONITOR_INTERVAL))
    # 제외는 tail 보다 먼저 적용한다 — 제외 대상 20줄이 진짜 오류를 밀어내면 안 된다.
    MONITOR_LOGS="$(docker logs "$MONITOR_CONTAINER" --since "$MONITOR_SINCE" 2>&1 || true)"
    MONITOR_RAW="$(printf '%s\n' "$MONITOR_LOGS" | grep -E "$MONITOR_PATTERN" || true)"
    MONITOR_SUPPRESSED=0
    if [[ -n "$MONITOR_RAW" ]]; then
        MONITOR_SUPPRESSED="$(printf '%s\n' "$MONITOR_RAW" | grep -Ec "$MONITOR_EXCLUDE_PATTERN" || true)"
    fi
    MONITOR_HITS="$(printf '%s\n' "$MONITOR_RAW" | grep -Ev "$MONITOR_EXCLUDE_PATTERN" | grep -E "$MONITOR_PATTERN" | tail -20 || true)"
    if [[ "$MONITOR_SUPPRESSED" != "0" ]]; then
        echo "[deploy.sh] Phase 7: 사용자 SQL 오류 ${MONITOR_SUPPRESSED}건 제외 (릴리스 실패 사유 아님)"
    fi
    deploy_observe_update "verifying" "p0p1_monitoring" \
        "elapsed=${MONITOR_ELAPSED}s; max=${MONITOR_SECONDS}s; suppressed_user_sql=${MONITOR_SUPPRESSED}"
    if [[ -n "$MONITOR_HITS" ]]; then
        echo "[deploy.sh] Phase 7: P0/P1 의심 로그 감지 — 구 슬롯 기준선과 비교"
        echo "$MONITOR_HITS"
        MONITOR_OBSERVED="$(printf '%s\n' "$MONITOR_LOGS" | p0p1_event_counts "$MONITOR_PATTERN" "$MONITOR_EXCLUDE_PATTERN" || true)"
        if [[ -z "$MONITOR_OBSERVED" ]]; then
            # 로그에는 오류가 있는데 키를 못 뽑았다 — 분류 불능은 소음이 아니라 실패다.
            MONITOR_VERDICT="FAIL_UNPARSED hits=unclassifiable"
        else
            MONITOR_VERDICT="$(p0p1_classify_hits "$P0P1_BASELINE_ROWS" "$MONITOR_OBSERVED" "$(( $(date +%s) - MONITOR_SINCE_EPOCH ))" || true)"
        fi
        echo "$MONITOR_VERDICT" | sed 's/^/[deploy.sh] Phase 7: 판정 /'
        MONITOR_FAIL_LINES="$(grep '^FAIL_' <<<"$MONITOR_VERDICT" || true)"
        if [[ -n "$MONITOR_FAIL_LINES" ]]; then
            p0p1_monitor_fail "P0/P1 monitor regression" "${MONITOR_FAIL_LINES//$'\n'/ | } || ${MONITOR_HITS}"
        fi
        P0P1_NOISE_NOTE="p0p1 baseline noise (not a failure): $(grep '^NOISE ' <<<"$MONITOR_VERDICT" | sed 's/^NOISE //' | tr '\n' ',' | head -c 300)"
        echo "[deploy.sh] ⚠️ Phase 7: 구 슬롯에도 있던 기존 소음만 감지 — 실패로 보지 않음"
        if [[ "${P0P1_NOISE_NOTIFIED:-false}" != "true" ]]; then
            P0P1_NOISE_NOTIFIED=true
            notify "⚠️ 배포 후 P0/P1 감시: 기존 소음만 감지(구 슬롯 기준선 이내) — 통과"
        fi
    fi
    # 로그만 보면 "조용한 실패"(프로세스는 살아 있는데 응답을 못 하는 상태)를
    # 놓친다. 헬스까지 같이 확인해 검증을 넓히고, 그 대신 전 구간이 깨끗하면
    # 최소 관측 시간만 채우고 끝낸다. 300초 고정 대기는 배포 시간의 25%였다.
    if ! curl -sf --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
        echo "[deploy.sh] ❌ Phase 7: health check 실패 (${MONITOR_ELAPSED}s)"
        p0p1_monitor_fail "Phase 7 health failed" "at ${MONITOR_ELAPSED}s ${HEALTH_URL}"
    fi
    echo "[deploy.sh] Phase 7: monitoring ${MONITOR_ELAPSED}/${MONITOR_SECONDS}초 이상 없음"
    if [[ "$MONITOR_ELAPSED" -ge "$MONITOR_MIN_SECONDS" ]]; then
        echo "[deploy.sh] Phase 7: ✅ ${MONITOR_ELAPSED}초 연속 이상 없음 — 조기 종료"
        audit_control "p0p1-monitor" "$MONITOR_CONTAINER" "success" \
            "early_exit_after=${MONITOR_ELAPSED}s; max=${MONITOR_SECONDS}s"
        break
    fi
done
deploy_phase_end "p0p1_monitoring" "success" "seconds=${MONITOR_ELAPSED}"

stop_downtime_monitor
resolve_final_deploy_status
if [[ "$FINAL_DEPLOY_STATUS" == "success_partial" ]]; then
    # Cutover is healthy, but the standby slot was not proven to run the same
    # immutable release image (active streams, skipped sync, or digest drift).
    # Keep the run explicitly uncertified until both digests match.
    echo "[deploy.sh] ⚠️ 트래픽 전환 성공, 릴리스 인증 보류 — standby same-digest 미충족 (${FINAL_DEPLOY_ERROR})"
    notify "⚠️ 트래픽 전환 성공, 릴리스 인증 보류 — standby 동기화 필요"
else
    echo "[deploy.sh] ✅ 배포 완료 — 필수 검증 통과 (mode=${MODE}, frontend_qa=${FRONTEND_QA_STATUS})"
    notify "✅ 배포 완료 — 필수 검증 통과 (mode=${MODE}, frontend_qa=${FRONTEND_QA_STATUS})"
fi
for _final_try in 1 2 3; do
    deploy_observe_update "$FINAL_DEPLOY_STATUS" "completed" "$FINAL_DEPLOY_ERROR"
    _final_status="$(deploy_db_exec "SELECT status FROM deploy_runs WHERE id=${DEPLOY_RUN_ID}")"
    if [[ "${_final_status:-}" == "$FINAL_DEPLOY_STATUS" ]]; then
        break
    fi
    echo "[deploy.sh] ⚠️ final status DB update retry ${_final_try}/3 (expected=${FINAL_DEPLOY_STATUS}; got=${_final_status:-empty})"
    sleep 2
done
record_deploy "$FINAL_DEPLOY_STATUS" "$MODE" "$FINAL_DEPLOY_ERROR"
# RC8: persist the chosen terminal state without upgrading an uncertified
# success_partial run to success.
for _final_retry in 1 2 3; do
    _final_error_sql="$(sql_escape "$FINAL_DEPLOY_ERROR")"
    deploy_db_exec "UPDATE deploy_runs SET status='${FINAL_DEPLOY_STATUS}', phase='completed', phase_completed_at=NOW(), updated_at=NOW(), last_heartbeat_at=NOW(), error_summary=NULLIF('${_final_error_sql}', '') WHERE id=${DEPLOY_RUN_ID} AND status != '${FINAL_DEPLOY_STATUS}';" >/dev/null 2>&1 && break
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
if [[ "$FINAL_DEPLOY_STATUS" == "success" ]] \
    && [[ -x "${COMPOSE_DIR}/scripts/record-release-provenance.sh" ]] \
    && [[ -n "${DEPLOY_RUN_ID:-}" ]]; then
    "${COMPOSE_DIR}/scripts/record-release-provenance.sh" \
        --repo "$COMPOSE_DIR" \
        --deploy-run-id "$DEPLOY_RUN_ID" \
        --project "AADS" \
        --component "api" \
        --release-ref "HEAD" || echo "[deploy.sh] ⚠️ release provenance not recorded (non-fatal)"
fi

start_deploy_queue_worker "post_success"
exit 0
