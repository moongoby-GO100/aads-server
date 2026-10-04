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

# 설치 원본은 항상 origin/main export 여야 한다 (AADS-RUNNER-SYNC-SOURCE-ORIGIN-MAIN).
# 공유 체크아웃의 작업본/HEAD 는 다른 세션이 쓰는 중이라 62커밋 뒤처지거나 미커밋일 수 있고,
# 그 파일로 원격 러너를 덮으면 최신 정본을 옛 것으로 되돌린다. 직접 실행해도 런처를 거쳐
# export 안의 이 스크립트가 다시 실행되도록 한다. 런처가 AADS_RUNNER_SYNC_EXPORTED=1 을 건다.
if [[ "${AADS_RUNNER_SYNC_EXPORTED:-0}" != "1" ]]; then
    case " $* " in
        *" -h "*|*" --help "*) ;;
        *) exec bash "${SCRIPT_DIR}/runner_sync_launcher.sh" "$@" ;;
    esac
fi

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
# 호스트별 결과 분리 (AADS-LLM-M6-DEPLOY-REGRESSION-GUARD, 2026-09-29).
# jinah244 는 root 접근이 거부된다("mkdir: '/root' 디렉터리를 만들 수 없습니다: 허가 거부").
# 예전에는 그 한 건이 set -e 로 스크립트 전체를 exit 1 로 끝내 유닛이 상시 failed 였고,
# 러너 수정이 각 호스트에 닿았는지 판정할 신호가 죽어 있었다.
#   synced   — 설치/검증 완료(이미 최신 포함)
#   deferred — 러너 작업 중이라 의도적으로 미룸
#   skipped  — 설정으로 제외했거나 접근 거부(구조적으로 동기화 대상 아님). 종료코드 미반영
#   failed   — 도달 가능한 호스트에서 실제 실패. 하나라도 있으면 exit 1
SKIP_TARGETS="${AADS_RUNNER_SYNC_SKIP_TARGETS:-}"
ACCESS_DENIED_RE='Permission denied|허가 거부|Operation not permitted|명령을 허용하지 않음'
TARGET_STATE_FILE=""
# EXIT trap 은 main 이 끝난 뒤 돈다 — local 이면 set -u 에서 unbound 로 exit 1 이 된다.
TARGET_ERR_FILE=""
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
  AADS_RUNNER_SYNC_SKIP_TARGETS target names excluded from sync (comma/space separated)
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
EOF
    # jinah244 는 2026-10-02 CEO 결정("진아서버는 앞으로 이용 안 한다")으로 기본 동기화 대상에서 뺐다.
    # 되살리려면 AADS_RUNNER_SYNC_TARGETS 에 아래 레코드를 넘기거나 위 heredoc 에 행으로 복원한다.
    #   jinah244|jinah244|/root/scripts/pipeline-runner.sh|aads-pipeline-runner.service|${SCRIPT_DIR}/aads-pipeline-runner.244.service
}

is_skip_target() {
    local name="$1" item
    for item in ${SKIP_TARGETS//,/ }; do
        [[ "$item" == "$name" ]] && return 0
    done
    return 1
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
        [[ -n "$TARGET_STATE_FILE" ]] && printf 'deferred' > "$TARGET_STATE_FILE"
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

    # Claude CLI json 영수증 파서(37203be4). 러너가 job 마다 subprocess 로 부르므로 재시작이
    # 필요 없다(changed 에 반영하지 않는다). 이 파일이 없으면 runner_cli_usage_ready 가 거짓이라
    # 원격 러너의 actual_model 이 영영 unverified 로 남는다 — 2026-10-02 contabo14 실측.
    local cli_usage_status=0
    sync_remote_file_if_changed "$name" "$host" "${SCRIPT_DIR}/runner_cli_usage.py" "$(dirname "$remote_runner")/runner_cli_usage.py" "0644" || cli_usage_status=$?
    [[ "$cli_usage_status" == "0" || "$cli_usage_status" == "1" ]] || return "$cli_usage_status"

    # worktree 회수 스크립트. 러너가 자기 디렉터리에서 이 이름으로 찾는다(_reclaimer_script_path).
    # 없으면 5분 정리와 즉시 회수가 모두 멈춘다 — 2026-10-05 contabo14 실측(/tmp/aads-wt-runner-* 52개 11.5GB).
    # 러너가 job 마다 bash 로 새로 실행하므로 갱신에 재시작이 필요 없다(changed 에 반영하지 않는다).
    # 옛 런처가 export 하지 않은 경우(원본 없음)는 실패시키지 않고 경고만 남긴다 — 런처 재설치가 필요하다.
    local reclaim_src="${SCRIPT_DIR}/reclaim_runner_worktrees.sh"
    if [[ -s "$reclaim_src" ]]; then
        bash -n "$reclaim_src"
        local reclaim_status=0
        sync_remote_file_if_changed "$name" "$host" "$reclaim_src" "$(dirname "$remote_runner")/reclaim_runner_worktrees.sh" "0755" || reclaim_status=$?
        [[ "$reclaim_status" == "0" || "$reclaim_status" == "1" ]] || return "$reclaim_status"
    else
        log "WARN ${name}: reclaim_runner_worktrees.sh 원본 없음(${reclaim_src}) — 설치된 런처(/usr/local/sbin/aads-runner-sync-launcher)가 옛 버전이면 재설치하라. 원격 worktree 회수가 동작하지 않는다"
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
    # 원본은 런처가 만든 origin/main export 라 항상 커밋본이다 — 작업본 비교 게이트는 없다.
    log "source=${AADS_RUNNER_SYNC_SOURCE_SHA:-unknown} root=${REPO_ROOT}"
    [[ -f "$CANONICAL_RUNNER" ]] || { echo "ERROR: canonical runner missing: $CANONICAL_RUNNER" >&2; exit 2; }
    bash -n "$CANONICAL_RUNNER"

    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        log "another sync is already running"
        exit 0
    fi

    local targets
    targets="${AADS_RUNNER_SYNC_TARGETS:-$(default_targets)}"
    local matched=0 synced=0 skipped=0 failed=0 target name rc state summary=""
    TARGET_ERR_FILE=$(mktemp)
    TARGET_STATE_FILE=$(mktemp)
    trap 'rm -f "$TARGET_ERR_FILE" "$TARGET_STATE_FILE"' EXIT
    while IFS= read -r target || [[ -n "$target" ]]; do
        [[ -n "${target//[[:space:]]/}" ]] || continue
        name="${target%%|*}"
        if [[ -n "$ONLY_TARGET" && "$ONLY_TARGET" != "$name" ]]; then
            continue
        fi
        matched=$((matched + 1))
        if is_skip_target "$name"; then
            log "${name}: skipped — AADS_RUNNER_SYNC_SKIP_TARGETS 로 제외"
            skipped=$((skipped + 1))
            summary+=" ${name}=skipped(config)"
            continue
        fi
        # 호스트 하나의 실패가 다른 호스트 동기화나 전체 종료코드를 끌고 가지 않게
        # 서브셸로 격리한다. 서브셸 안에서는 set -e 가 그대로 fail-closed 로 동작한다.
        : > "$TARGET_STATE_FILE"
        set +e
        ( set -e; sync_one_target "$target" ) 2>"$TARGET_ERR_FILE"
        rc=$?
        set -e
        [[ -s "$TARGET_ERR_FILE" ]] && cat "$TARGET_ERR_FILE" >&2
        state=$(cat "$TARGET_STATE_FILE" 2>/dev/null || true)
        if [[ "$rc" == "0" && "$state" == "deferred" ]]; then
            DEFERRED=$((DEFERRED + 1))
            summary+=" ${name}=deferred"
        elif [[ "$rc" == "0" ]]; then
            synced=$((synced + 1))
            summary+=" ${name}=synced"
        elif grep -Eq "$ACCESS_DENIED_RE" "$TARGET_ERR_FILE"; then
            log "WARN ${name}: skipped — 접근 거부(rc=${rc}). 구조적으로 대상이 아니면 AADS_RUNNER_SYNC_SKIP_TARGETS 에 추가하라"
            skipped=$((skipped + 1))
            summary+=" ${name}=skipped(access_denied)"
        else
            log "ERROR ${name}: sync failed rc=${rc}"
            failed=$((failed + 1))
            summary+=" ${name}=failed(rc=${rc})"
        fi
    done <<< "$targets"

    if [[ "$matched" -eq 0 ]]; then
        echo "ERROR: no targets matched" >&2
        exit 2
    fi
    log "sync summary:${summary}"
    log "sync complete targets=${matched} synced=${synced} deferred=${DEFERRED} skipped=${skipped} failed=${failed}"
    if [[ "$failed" -gt 0 ]]; then
        exit 1
    fi
}

main "$@"
