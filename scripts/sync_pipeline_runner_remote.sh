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
  --ignore-busy                 override the DB busy count only after the live
                                process probe has verified an empty worker set

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

remote_runner_process_state() {
    local host="$1" service="$2" payload state=""
    [[ "$service" =~ ^[A-Za-z0-9_.@:-]+$ ]] || { printf UNKNOWN; return 0; }
    payload="$(declare -f runner_live_process_probe)"$'\n''runner_live_process_probe "$1"'
    # No remote files are installed for the probe. The existing launcher
    # already exports runner_busy_lib.sh from the same source SHA.
    state=$(ssh "${SSH_OPTS[@]}" "$host" "bash -s -- '$service'" <<< "$payload" 2>/dev/null) || state="UNKNOWN"
    case "$state" in IDLE|BUSY|UNKNOWN) printf '%s' "$state" ;; *) printf UNKNOWN ;; esac
}

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

remote_runner_maintenance_state() {
    local host="$1" service="$2" payload state=""
    [[ "$service" =~ ^[A-Za-z0-9_.@:-]+$ ]] || { printf UNKNOWN; return 0; }
    payload="$(declare -f runner_maintenance_metadata)"$'\n''runner_maintenance_metadata preflight /run/aads-runner-maintenance "$1"'
    state=$(ssh "${SSH_OPTS[@]}" "$host" "bash -s -- '$service'" <<< "$payload" 2>/dev/null) || state="UNKNOWN"
    [[ "$state" == AWARE ]] && printf AWARE || printf BOOTSTRAP_REQUIRED
}

# Executed on the target host, with both maintenance EX leases inherited.
# Only regular, checksum-pinned bundle members are accepted. All active paths
# change in this one process; there is no SCP/install/restart lease gap.
runner_apply_bundle() {
    local service="$1" archive="$2" expected="$3" remote_runner="$4" restart="$5"
    local changed restart_required restart_plan
    read -r changed restart_required < <(python3 - "$archive" "$expected" "$remote_runner" "$service" <<'PY_RUNNER_BUNDLE'
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

archive, expected, runner, service = sys.argv[1:]
runner = Path(runner)
if not runner.is_absolute() or '..' in runner.parts:
    raise SystemExit('invalid runner destination')
if hashlib.sha256(Path(archive).read_bytes()).hexdigest() != expected:
    raise SystemExit('bundle hash mismatch')
allowed = {str(runner), *(str(runner.parent / name) for name in (
    'runner_busy_lib.sh', 'claude_model_contract.py', 'runner_cli_usage.py',
    'reclaim_runner_worktrees.sh', 'aag-brief.py')), '/etc/systemd/system/' + service}
with tempfile.TemporaryDirectory(prefix='aads-runner-apply-') as directory:
    staging = Path(directory)
    with tarfile.open(archive, 'r') as bundle:
        members = bundle.getmembers()
        if any(not member.isfile() or '/' in member.name or member.name in ('.', '..') for member in members):
            raise SystemExit('unsafe bundle member')
        if len({member.name for member in members}) != len(members):
            raise SystemExit('duplicate bundle member')
        manifest = json.load(bundle.extractfile('manifest.json'))
        if set(manifest) != {'files'} or not isinstance(manifest['files'], list):
            raise SystemExit('invalid bundle manifest')
        entries = manifest['files']
        if {member.name for member in members} != {'manifest.json', *(row['member'] for row in entries)}:
            raise SystemExit('unexpected bundle member')
        if len({row['destination'] for row in entries}) != len(entries):
            raise SystemExit('duplicate destination')
        prepared = []
        for index, row in enumerate(entries):
            if set(row) != {'member', 'destination', 'sha256', 'mode', 'restart'}:
                raise SystemExit('invalid bundle row')
            if row['destination'] not in allowed or row['mode'] not in (0o644, 0o755):
                raise SystemExit('unapproved bundle destination/mode')
            data = bundle.extractfile(row['member']).read()
            if hashlib.sha256(data).hexdigest() != row['sha256']:
                raise SystemExit('member hash mismatch')
            staged = staging / str(index)
            staged.write_bytes(data)
            if row['destination'].endswith('.sh'):
                subprocess.run(['bash', '-n', str(staged)], check=True)
            target = Path(row['destination'])
            if target.is_symlink():
                raise SystemExit('symlink destination')
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == row['sha256']:
                continue
            prepared.append((row, staged, target))
    applied = []
    try:
        for index, (row, staged, target) in enumerate(prepared):
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = staging / ('backup-' + str(index))
            existed = target.exists()
            if existed:
                shutil.copy2(target, backup)
                if target == runner:
                    retained = target.with_name(target.name + '.bak.maintenance.' + expected[:12])
                    if retained.is_symlink():
                        raise RuntimeError('symlink backup')
                    if not retained.exists():
                        shutil.copy2(target, retained)
            fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '.maintenance-', dir=target.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(staged.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, row['mode'])
                os.replace(temporary, target)  # Existing Bash inode remains intact.
                applied.append((target, backup, existed))
            finally:
                Path(temporary).unlink(missing_ok=True)
            if hashlib.sha256(target.read_bytes()).hexdigest() != row['sha256']:
                raise RuntimeError('installed hash mismatch')
    except BaseException:
        for target, backup, existed in reversed(applied):
            if existed:
                fd, restore = tempfile.mkstemp(prefix='.' + target.name + '.restore-', dir=target.parent)
                os.close(fd)
                try:
                    shutil.copy2(backup, restore)
                    os.replace(restore, target)
                finally:
                    Path(restore).unlink(missing_ok=True)
            else:
                target.unlink(missing_ok=True)
        raise
    print(int(bool(prepared)), int(any(row['restart'] for row, _, _ in prepared)))
PY_RUNNER_BUNDLE
    ) || return 1
    [[ "$changed" =~ ^[01]$ && "$restart_required" =~ ^[01]$ ]] || return 1
    if [[ "$restart" == 1 && "$restart_required" == 1 ]]; then
        systemctl daemon-reload || return 1
        restart_plan=$(runner_maintenance_metadata restart-plan /run/aads-runner-maintenance "$service") || return 1
        systemctl restart "$service" || return 1
        systemctl is-active "$service" || return 1
        [[ "$(runner_maintenance_metadata restart-verify /run/aads-runner-maintenance "$service" "$restart_plan")" == AWARE ]] || return 1
    fi
    printf 'MAINTENANCE_APPLIED changed=%s restart=%s\n' "$changed" "$restart_required"
}

make_runner_bundle() {
    local archive="$1" remote_runner="$2" service="$3" service_unit="$4"
    python3 - "$archive" "$remote_runner" "$service" "$service_unit" "$CANONICAL_RUNNER" "$SCRIPT_DIR" "$REPO_ROOT" "$SYNC_REMOTE_UNITS" <<'PY_RUNNER_BUNDLE_CREATE'
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
archive, runner, service, unit, canonical, script_dir, repo, units = sys.argv[1:]
script_dir, directory = Path(script_dir), Path(runner).parent
rows = [(script_dir / 'runner_busy_lib.sh', directory / 'runner_busy_lib.sh', 0o644, True),
        (script_dir / 'claude_model_contract.py', directory / 'claude_model_contract.py', 0o644, True),
        (Path(repo) / 'tools/aag/brief.py', directory / 'aag-brief.py', 0o644, False),
        (script_dir / 'runner_cli_usage.py', directory / 'runner_cli_usage.py', 0o644, False)]
reclaim = script_dir / 'reclaim_runner_worktrees.sh'
if reclaim.is_file():
    rows.append((reclaim, directory / reclaim.name, 0o755, False))
rows.append((Path(canonical), Path(runner), 0o755, True))
if units == '1':
    rows.append((Path(unit), Path('/etc/systemd/system') / service, 0o644, True))
manifest = []
with tarfile.open(archive, 'w') as bundle:
    for index, (source, destination, mode, restart) in enumerate(rows):
        data = source.read_bytes()
        member = 'file-' + str(index)
        info = tarfile.TarInfo(member); info.size = len(data); info.mode = 0o600
        bundle.addfile(info, io.BytesIO(data))
        manifest.append(dict(member=member, destination=str(destination), mode=mode, restart=restart,
                             sha256=hashlib.sha256(data).hexdigest()))
    data = json.dumps(dict(files=manifest), sort_keys=True).encode()
    info = tarfile.TarInfo('manifest.json'); info.size = len(data); info.mode = 0o600
    bundle.addfile(info, io.BytesIO(data))
PY_RUNNER_BUNDLE_CREATE
}

runner_bundle_matches_remote() {
    local archive="$1" host="$2" rows destination expected actual
    rows=$(python3 - "$archive" <<'PY_RUNNER_BUNDLE_COMPARE'
import json
import sys
import tarfile
with tarfile.open(sys.argv[1]) as bundle:
    for row in json.load(bundle.extractfile('manifest.json'))['files']:
        print(row['destination'] + '\t' + row['sha256'])
PY_RUNNER_BUNDLE_COMPARE
    ) || return 1
    [[ -n "$rows" ]] || return 1
    while IFS=$'\t' read -r destination expected; do
        actual=$(remote_sha "$host" "$destination") || return 1
        [[ "$actual" == "$expected" ]] || return 1
    done <<< "$rows"
}

sync_one_target() {
    local record="$1"
    local name host remote_runner service service_unit
    IFS='|' read -r name host remote_runner service service_unit <<< "$record"
    [[ "$name" =~ ^[A-Za-z0-9._-]+$ && -n "$host" && "$remote_runner" =~ ^/[A-Za-z0-9_./-]+$ && "$service" =~ ^[A-Za-z0-9_.@:-]+$ ]] || {
        echo "ERROR: invalid target record" >&2
        return 2
    }
    [[ -z "$ONLY_TARGET" || "$ONLY_TARGET" == "$name" ]] || return 0
    if [[ "$SYNC_REMOTE_UNITS" == 1 && ! -f "$service_unit" ]]; then
        echo "ERROR: service unit missing for ${name}" >&2
        return 2
    fi
    # Preserve the conservative observation prefilter. Passing it is NEVER an
    # authorization: only the later two-lock transaction can install/restart.
    local process_state maintenance_state runner_host_name busy_count service_state
    process_state=$(remote_runner_process_state "$host" "$service")
    if should_defer_for_processes "$process_state"; then
        log "${name}: sync deferred — live process state=${process_state}; DB/ignore-busy cannot override"
        [[ -n "$TARGET_STATE_FILE" ]] && printf deferred > "$TARGET_STATE_FILE"
        return 0
    fi
    maintenance_state=$(remote_runner_maintenance_state "$host" "$service")
    if [[ "$maintenance_state" != AWARE ]]; then
        log "${name}: sync deferred — BOOTSTRAP_REQUIRED; legacy actors cannot honor maintenance locks"
        [[ -n "$TARGET_STATE_FILE" ]] && printf deferred > "$TARGET_STATE_FILE"
        return 0
    fi
    runner_host_name=$(remote_runner_host_name "$host" "$service")
    busy_count=$(db_active_job_count "$runner_host_name")
    service_state=$(remote_service_active "$host" "$service")
    if should_defer_for_busy "$busy_count" "$IGNORE_BUSY" "$service_state"; then
        log "${name}: sync deferred — host=${runner_host_name:-unknown} jobs=${busy_count:-unknown} service=${service_state:-unknown}"
        [[ -n "$TARGET_STATE_FILE" ]] && printf deferred > "$TARGET_STATE_FILE"
        return 0
    fi
    if [[ "$DRY_RUN" == 1 ]]; then
        log "DRY_RUN ${name}: would acquire maintenance leases, verify bundle, and apply in one remote transaction"
        return 0
    fi
    local archive expected remote_archive payload rc=0
    archive=$(mktemp /tmp/aads-runner-bundle.XXXXXXXX.tar) || return 1
    make_runner_bundle "$archive" "$remote_runner" "$service" "$service_unit" || { rm -f "$archive"; return 1; }
    if runner_bundle_matches_remote "$archive" "$host"; then
        rm -f "$archive"
        log "${name}: already synced"
        return 0
    fi
    expected=$(sha256_file "$archive")
    remote_archive="/tmp/$(basename "$archive")"
    scp_put "$archive" "$host" "$remote_archive" || { rm -f "$archive"; return 1; }
    rm -f "$archive"
    payload="$(declare -f runner_maintenance_metadata runner_maintenance_transaction runner_apply_bundle)"
    payload+=$'\n''trap '\''rm -f "$2"'\'' EXIT'
    payload+=$'\n''runner_maintenance_transaction "$1" 30 /run/aads-runner-maintenance runner_apply_bundle "$@"'
    # A single SSH process owns admission EX, lifetime EX, installation and
    # restart. Losing this connection cannot release leases held by apply children.
    ssh "${SSH_OPTS[@]}" "$host" "bash -s -- '$service' '$remote_archive' '$expected' '$remote_runner' '$RESTART_SERVICES'" <<< "$payload" || rc=$?
    if [[ "$rc" == 3 ]]; then
        log "${name}: sync deferred — maintenance lease/capability could not be verified"
        [[ -n "$TARGET_STATE_FILE" ]] && printf deferred > "$TARGET_STATE_FILE"
        return 0
    fi
    [[ "$rc" == 0 ]] || return "$rc"
    log "${name}: synced checksum-pinned bundle under exclusive maintenance lease"
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
