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
PG_CONTAINER="${PG_CONTAINER:-aads-postgres}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"

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

# 해당 러너 호스트가 붙잡고 있는 작업 수. 조회 실패/이름 미상이면 빈 문자열.
db_active_job_count() {
    local host_name="$1" out=""
    [[ -n "$host_name" ]] || { printf ''; return 0; }
    out=$(docker exec -i "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" \
            -q -t -A -P footer=off \
            -c "SELECT count(*) FROM pipeline_jobs WHERE status IN ('claimed','running','deploying') AND runner_host='${host_name}';" \
            </dev/null 2>/dev/null | tr -d '[:space:]') || out=""
    [[ "$out" =~ ^[0-9]+$ ]] || out=""
    printf '%s' "$out"
}

# 0 = 미루기, 1 = 진행.
# 판별 불가(빈 문자열)도 미룬다 — 타이머가 5분마다 다시 시도하므로 비용은 지연뿐이고,
# 잘못 진행하면 실행 중인 P0 작업이 처음부터 다시 돌아간다.
should_defer_for_busy() {
    local busy_count="$1" ignore_busy="${2:-0}"
    [[ "$ignore_busy" == "1" ]] && return 1
    [[ "$busy_count" =~ ^[0-9]+$ ]] || return 0
    [[ "$busy_count" -gt 0 ]] && return 0
    return 1
}

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
    local runner_host_name busy_count
    runner_host_name=$(remote_runner_host_name "$host" "$service")
    busy_count=$(db_active_job_count "$runner_host_name")
    if should_defer_for_busy "$busy_count" "$IGNORE_BUSY"; then
        log "${name}: sync deferred — runner host=${runner_host_name:-unknown} active_jobs=${busy_count:-unknown}"
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
    for source_file in scripts/pipeline-runner.sh scripts/claude_model_contract.py scripts/sync_pipeline_runner_remote.sh; do
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
