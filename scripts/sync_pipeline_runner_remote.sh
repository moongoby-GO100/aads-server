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

usage() {
    cat <<'EOF'
Usage: sync_pipeline_runner_remote.sh [--dry-run] [--no-restart] [--target NAME]

Targets:
  contabo14  /root/scripts/pipeline-runner.sh  aads-pipeline-runner.service
  cafe24_114 /root/scripts/pipeline-runner.sh  aads-pipeline-litellm-runner.service

Environment:
  CANONICAL_RUNNER              local canonical runner script
  AADS_RUNNER_SYNC_UNITS        set to 1 to also install remote service units
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

default_targets() {
    cat <<EOF
contabo14|contabo14|/root/scripts/pipeline-runner.sh|aads-pipeline-runner.service|${SCRIPT_DIR}/aads-pipeline-litellm-runner.211.service
cafe24_114|server-114|/root/scripts/pipeline-runner.sh|aads-pipeline-litellm-runner.service|${SCRIPT_DIR}/aads-pipeline-litellm-runner.114.service
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

    local local_sha current_sha installed_sha unit_dest changed=0 unit_changed=0
    local_sha=$(sha256_file "$CANONICAL_RUNNER")
    current_sha=$(remote_sha "$host" "$remote_runner" | tr -d '[:space:]')
    unit_dest="/etc/systemd/system/${service}"

    log "${name}: current=${current_sha:-missing} desired=${local_sha}"
    ssh_run "$host" "mkdir -p '$(dirname "$remote_runner")'"

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
    log "sync complete targets=${synced}"
}

main "$@"
