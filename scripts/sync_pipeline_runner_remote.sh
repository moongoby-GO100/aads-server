#!/usr/bin/env bash
# Synchronize the canonical Pipeline Runner script from AADS to remote runner hosts.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CANONICAL_RUNNER="${CANONICAL_RUNNER:-${SCRIPT_DIR}/pipeline-runner.sh}"
LOCK_FILE="${AADS_RUNNER_SYNC_LOCK:-/tmp/aads-pipeline-runner-sync.lock}"
SSH_OPTS=(
    -o BatchMode=yes
    -o StrictHostKeyChecking=no
    -o ConnectTimeout=15
    -o ServerAliveInterval=30
    -o ServerAliveCountMax=2
)
SCP_OPTS=(
    -o BatchMode=yes
    -o StrictHostKeyChecking=no
    -o ConnectTimeout=15
)

DRY_RUN=0
RESTART_SERVICES=1
SYNC_REMOTE_UNITS="${AADS_RUNNER_SYNC_UNITS:-0}"
ONLY_TARGET=""
# 원격 러너가 작업 실행 중이면 설치와 재시작을 모두 미룬다
# (AADS-RUNNER-SYNC-BUSY-DEFER, 2026-09-16).
# 근거: 10:28:51 KST 동기화 재시작이 GO100 runner-1791da41(P0) 을
# runner_shutdown_requeued 로 되돌려 6분 16초치 LLM 작업을 처음부터 다시 시켰다.
# 실행 중 스크립트 파일을 덮어쓰는 것 자체도 bash 지연 읽기 때문에 위험하다.
IGNORE_BUSY="${AADS_RUNNER_SYNC_IGNORE_BUSY:-0}"
DEFERRED=0
# DB 접속 기본값은 runner_busy_lib.sh 가 소유한다(두 벌로 두지 않는다).

usage() {
    cat <<'EOF'
Usage: sync_pipeline_runner_remote.sh [--dry-run] [--no-restart] [--target NAME] [--ignore-busy]

Targets:
  contabo14  /root/scripts/pipeline-runner.sh  aads-pipeline-runner.service
  cafe24_114 /root/scripts/pipeline-runner.sh  aads-pipeline-litellm-runner.service

Options:
  --ignore-busy                 sync even if the remote runner has in-flight jobs
                                (this requeues them — use only when the runner is stuck)

Environment:
  CANONICAL_RUNNER              local canonical runner script
  AADS_RUNNER_SYNC_UNITS        set to 1 to also install remote service units
  AADS_RUNNER_SYNC_IGNORE_BUSY  same as --ignore-busy
  AADS_RUNNER_SYNC_TARGETS      optional newline target records:
                                name|ssh_host|remote_runner|service|service_unit
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --no-restart)
            RESTART_SERVICES=0
            shift
            ;;
        --ignore-busy)
            IGNORE_BUSY=1
            shift
            ;;
        --target)
            ONLY_TARGET="${2:-}"
            [[ -n "$ONLY_TARGET" ]] || { echo "ERROR: --target requires a value" >&2; exit 2; }
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

log() {
    printf '[%s] %s\n' "$(TZ=Asia/Seoul date '+%F %T KST')" "$*"
}

sha256_file() {
    sha256sum "$1" | awk '{print $1}'
}

ssh_run() {
    local host="$1"
    shift
    ssh -n "${SSH_OPTS[@]}" "$host" "$@"
}

scp_put() {
    local src="$1" host="$2" dest="$3"
    scp "${SCP_OPTS[@]}" "$src" "${host}:${dest}"
}

remote_sha() {
    local host="$1" path="$2"
    ssh_run "$host" "sha256sum '$path' 2>/dev/null | awk '{print \$1}' || true"
}

# ── 실행중 러너 보호 (AADS-RUNNER-SYNC-BUSY-DEFER) ──────────────────────
# 원격 러너가 pipeline_jobs.runner_host 에 쓰는 이름은 systemd 환경변수
# AADS_RUNNER_HOST_NAME, 없으면 `hostname -s` 다. 동기화 대상 이름(cafe24_114)과
# 다를 수 있으므로(실측: cafe24_114 → rfree-0009) 하드코딩하지 않고 호스트에 묻는다.
remote_runner_host_name() {
    local host="$1" service="$2" name=""
    # 한 번의 SSH 로 끝낸다 — 환경변수 값이 있으면 그것이, 없으면 hostname -s 가 첫 비어있지 않은 줄이다.
    name=$(ssh_run "$host" "systemctl show -p Environment --value '$service' 2>/dev/null | tr ' ' '\n' | sed -n 's/^AADS_RUNNER_HOST_NAME=//p' | head -1; hostname -s" 2>/dev/null | awk 'NF{print; exit}' | tr -d '[:space:]') || name=""
    [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || name=""
    printf '%s' "$name"
}

# 러너 서비스가 지금 살아 있는가. inactive/failed 면 그 호스트의 in-flight 는 좀비다.
# `|| true` 는 원격에서 붙인다 — systemctl is-active 는 비활성일 때 exit 3 이라
# 로컬에서 `|| state=""` 로 받으면 "inactive" 문자열까지 지워진다.
remote_service_active() {
    local host="$1" service="$2" state=""
    state=$(ssh_run "$host" "systemctl is-active '$service' 2>/dev/null || true" 2>/dev/null | tr -d '[:space:]') || state=""
    printf '%s' "$state"
}

# busy 판정 규칙(db_active_job_count / should_defer_for_busy)은 로컬 재시작
# 래퍼와 공유한다. 두 경로가 다른 답을 내면 한쪽이 반드시 사고를 낸다.
# shellcheck source=scripts/runner_busy_lib.sh
source "${SCRIPT_DIR}/runner_busy_lib.sh"

default_targets() {
    cat <<EOF
contabo14|contabo14|/root/scripts/pipeline-runner.sh|aads-pipeline-runner.service|${SCRIPT_DIR}/aads-pipeline-litellm-runner.211.service
cafe24_114|server-114|/root/scripts/pipeline-runner.sh|aads-pipeline-litellm-runner.service|${SCRIPT_DIR}/aads-pipeline-litellm-runner.114.service
jinah244|jinah244|/root/scripts/pipeline-runner.sh|aads-pipeline-runner.service|${SCRIPT_DIR}/aads-pipeline-runner.244.service
EOF
}

install_remote_file() {
    local name="$1" host="$2" src="$3" dest="$4" mode="$5"
    local tmp="/tmp/$(basename "$dest").aads-sync.$$"

    if [[ "$DRY_RUN" == "1" ]]; then
        log "DRY_RUN ${name}: would copy ${src} -> ${host}:${dest}"
        return 0
    fi

    scp_put "$src" "$host" "$tmp"
    ssh_run "$host" "install -m '$mode' '$tmp' '$dest' && rm -f '$tmp'"
}

sync_remote_file_if_changed() {
    local name="$1" host="$2" src="$3" dest="$4" mode="$5"
    local local_sha remote_current_sha remote_installed_sha
    local_sha=$(sha256_file "$src")
    remote_current_sha=$(remote_sha "$host" "$dest" | tr -d '[:space:]')
    if [[ "$remote_current_sha" == "$local_sha" ]]; then
        log "${name}: already synced ${dest}"
        return 1
    fi

    install_remote_file "$name" "$host" "$src" "$dest" "$mode"
    remote_installed_sha=$(remote_sha "$host" "$dest" | tr -d '[:space:]')
    if [[ "$DRY_RUN" != "1" && "$remote_installed_sha" != "$local_sha" ]]; then
        echo "ERROR: ${name} installed hash mismatch for ${dest}: ${remote_installed_sha:-missing} != ${local_sha}" >&2
        return 2
    fi
    return 0
}

sync_one_target() {
    local record="$1"
    local name host remote_runner service service_unit
    IFS='|' read -r name host remote_runner service service_unit <<< "$record"
    [[ -n "$name" && -n "$host" && -n "$remote_runner" && -n "$service" ]] || {
        echo "ERROR: invalid target record: $record" >&2
        return 2
    }
    if [[ -n "$ONLY_TARGET" && "$ONLY_TARGET" != "$name" ]]; then
        return 0
    fi
    if [[ "$SYNC_REMOTE_UNITS" == "1" && ! -f "$service_unit" ]]; then
        echo "ERROR: service unit missing for ${name}: ${service_unit}" >&2
        return 2
    fi

    # 실행 중인 러너는 건드리지 않는다 — 파일 교체도, 재시작도 미룬다.
    local runner_host_name busy_count service_state
    runner_host_name=$(remote_runner_host_name "$host" "$service")
    busy_count=$(db_active_job_count "$runner_host_name")
    service_state=$(remote_service_active "$host" "$service")
    if should_defer_for_busy "$busy_count" "$IGNORE_BUSY" "$service_state"; then
        log "${name}: sync deferred — runner host=${runner_host_name:-unknown} active_jobs=${busy_count:-unknown} service=${service_state:-unknown}"
        DEFERRED=$((DEFERRED + 1))
        return 0
    fi

    local local_sha current_sha installed_sha unit_dest changed=0 unit_changed=0
    local_sha=$(sha256_file "$CANONICAL_RUNNER")
    current_sha=$(remote_sha "$host" "$remote_runner" | tr -d '[:space:]')
    unit_dest="/etc/systemd/system/${service}"

    log "${name}: current=${current_sha:-missing} desired=${local_sha}"
    ssh_run "$host" "mkdir -p '$(dirname "$remote_runner")'"

    # Install the shared resolver before any runner referencing it can start.
    local contract_status=0
    sync_remote_file_if_changed "$name" "$host" "${SCRIPT_DIR}/claude_model_contract.py" "$(dirname "$remote_runner")/claude_model_contract.py" "0644" || contract_status=$?
    if [[ "$contract_status" == "0" ]]; then
        changed=1
    elif [[ "$contract_status" != "1" ]]; then
        return "$contract_status"
    fi

    # AAG 브리프 리더(호스트 공용, AADS-AAG-BRIEF-003) — subprocess 로 매 job 마다 fresh 실행되므로
    # 갱신에 재시작이 필요 없다(changed 에 반영하지 않는다). brief.py 는 프로젝트 저장소마다 복제하지 않는다.
    local aag_brief_status=0
    sync_remote_file_if_changed "$name" "$host" "${REPO_ROOT}/tools/aag/brief.py" "$(dirname "$remote_runner")/aag-brief.py" "0644" || aag_brief_status=$?
    [[ "$aag_brief_status" == "0" || "$aag_brief_status" == "1" ]] || return "$aag_brief_status"

    if [[ "$current_sha" != "$local_sha" ]]; then
        changed=1
        if [[ "$DRY_RUN" == "1" ]]; then
            log "DRY_RUN ${name}: would install runner script and keep backup"
        else
            local tmp="/tmp/pipeline-runner.sh.${local_sha}.$$"
            scp_put "$CANONICAL_RUNNER" "$host" "$tmp"
            ssh_run "$host" "bash -n '$tmp'"
            ssh_run "$host" "if [ -f '$remote_runner' ]; then cp -p '$remote_runner' '${remote_runner}.bak.aads-sync.$(TZ=Asia/Seoul date '+%Y%m%d%H%M%S')'; fi"
            ssh_run "$host" "install -m 0755 '$tmp' '$remote_runner' && rm -f '$tmp'"
        fi
    fi

    if [[ "$SYNC_REMOTE_UNITS" == "1" ]]; then
        if sync_remote_file_if_changed "$name" "$host" "$service_unit" "$unit_dest" "0644"; then
            unit_changed=1
        fi
    fi
    installed_sha=$(remote_sha "$host" "$remote_runner" | tr -d '[:space:]')
    if [[ "$installed_sha" != "$local_sha" ]]; then
        if [[ "$DRY_RUN" == "1" ]]; then
            log "DRY_RUN ${name}: would verify runner hash after install"
        else
            echo "ERROR: ${name} installed hash mismatch: ${installed_sha:-missing} != ${local_sha}" >&2
            return 1
        fi
    fi

    if [[ "$RESTART_SERVICES" == "1" && ( "$changed" == "1" || "$unit_changed" == "1" ) ]]; then
        if [[ "$DRY_RUN" == "1" ]]; then
            log "DRY_RUN ${name}: would daemon-reload and restart ${service}"
        else
            ssh_run "$host" "systemctl daemon-reload"
            ssh_run "$host" "systemctl restart '$service'"
            ssh_run "$host" "systemctl is-active '$service'"
        fi
    elif [[ "$RESTART_SERVICES" == "1" ]]; then
        if [[ "$DRY_RUN" == "1" ]]; then
            log "DRY_RUN ${name}: would skip restart because hashes are already current"
        else
            ssh_run "$host" "systemctl is-active '$service'"
        fi
    fi

    if [[ "$changed" == "1" ]]; then
        log "${name}: synced ${remote_runner} to ${local_sha}"
    else
        log "${name}: already synced"
    fi
    return 0
}

main() {
    # The timer must never publish edits from an in-progress shared worktree.
    local source_file committed_sha working_sha
    for source_file in scripts/pipeline-runner.sh scripts/claude_model_contract.py scripts/sync_pipeline_runner_remote.sh scripts/runner_busy_lib.sh tools/aag/brief.py; do
        committed_sha=$(git -C "$REPO_ROOT" show "HEAD:${source_file}" 2>/dev/null | sha256sum | awk '{print $1}') || {
            log "source not committed: ${source_file}; sync deferred"
            return 0
        }
        working_sha=$(sha256_file "${REPO_ROOT}/${source_file}")
        if [[ "$committed_sha" != "$working_sha" ]]; then
            log "source has uncommitted changes: ${source_file}; sync deferred"
            return 0
        fi
    done
    [[ -f "$CANONICAL_RUNNER" ]] || { echo "ERROR: canonical runner missing: $CANONICAL_RUNNER" >&2; exit 2; }
    bash -n "$CANONICAL_RUNNER"

    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log "another sync is already running"
        exit 0
    fi

    local targets
    targets="${AADS_RUNNER_SYNC_TARGETS:-$(default_targets)}"
    local synced=0
    while IFS= read -r target || [[ -n "$target" ]]; do
        [[ -n "${target//[[:space:]]/}" ]] || continue
        sync_one_target "$target"
        synced=$((synced + 1))
    done <<< "$targets"

    if [[ "$synced" -eq 0 ]]; then
        echo "ERROR: no targets matched" >&2
        exit 2
    fi
    log "sync complete targets=${synced} deferred=${DEFERRED}"
}

main "$@"
