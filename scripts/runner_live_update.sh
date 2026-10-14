#!/usr/bin/env bash
# Fail-closed runner-live controller with a gated, one-shot release apply path.
#
# Default behaviour is unchanged: with no release manifest, a legacy actor, an
# unknown preflight or an untrusted companion this controller only prints DEFER.
# A positive apply happens ONLY when a root-owned, schema-valid release manifest
# (/etc/aads/runner-live-release.json) matches ALL of: this controller's sha256,
# a pinned companion sha256, release_sha being an ancestor of origin/main, the
# runner-live HEAD being exactly expected_old_sha (CAS), a clean tree, an AWARE
# preflight, an idle runner, and both maintenance leases (admission + lifetime).
# The whole transaction runs under those leases: checkout, companion swap, the
# restart (restart_local_runner.sh --lease-held) and the new MainPID/source/HEAD
# check. The new daemon blocks in the admission flock until the leases are
# released, so the old daemon can never claim queued work in between. After the
# leases are released the new daemon's actor record must reach RUNNING with the
# expected source hash; otherwise old SHA/companion/daemon are restored in a
# second lease transaction. A manifest is consumed (renamed) after APPLIED or
# ROLLED_BACK.
#
# Design / install procedure: docs/design/20261014_AADS_러너라이브_정식반영경로_설계.md
# Install only from a committed/published source with expected-old checksum CAS
# and an atomic controller replacement (separate CEO approval). Installing this
# controller does not enroll a legacy Runner or establish a safe legacy bootstrap.
set -uo pipefail

RUNNER_LIVE_CONTROLLER_PATH="${BASH_SOURCE[0]}"
# sha256 values of runner_busy_lib.sh this controller trusts. A release that
# changes the companion must ship a controller whose list already contains the
# new hash (old + new during the transition), or the swapped companion would be
# untrusted by the very next run. See the design doc "companion 핀 갱신".
RUNNER_LIVE_COMPANION_PINS='78e55d7ad9dc00395ae3689f9ba374a21d7060cd7e6453bd7e8e1be62c3108ee'

# Bounded waits (seconds). Plain assignments: the entrypoint reads no environment.
RUNNER_LIVE_LEASE_WAIT=20
RUNNER_LIVE_ROLLBACK_LEASE_WAIT=60
RUNNER_LIVE_PID_WAIT=15
RUNNER_LIVE_RUNNING_WAIT=180

runner_live_defer() {
    local reason="$1"
    shift
    printf 'DEFER runner-live-update reason=%s' "$reason"
    (( $# )) && printf ' %s' "$@"
    printf '\n'
    return 0
}

runner_live_verified_companion() {
    # Return the verified bytes, not a path that could be replaced between a
    # checksum check and source. Never execute untrusted/unpinned companion code.
    # shellcheck disable=SC2086  # pins are hex words, intentionally split
    python3 - "$1" $RUNNER_LIVE_COMPANION_PINS <<'PY_RUNNER_LIVE_COMPANION'
import hashlib
import os
from pathlib import Path
import stat
import sys

expected = set(sys.argv[2:])
try:
    path = Path(sys.argv[1])
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute pinned path required')
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise ValueError('untrusted ownership/path')
        # Root-owned sticky parents (e.g. a test /tmp) cannot remove/replace
        # another root-owned child. The file itself can never be writable.
        sticky_parent = item != path and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        if info.st_mode & 0o022 and not sticky_parent:
            raise ValueError('writable path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('untrusted file')
        data = stream.read(1_048_577)
        if len(data) > 1_048_576 or hashlib.sha256(data).hexdigest() not in expected:
            raise ValueError('unpinned companion')
    sys.stdout.write(data.decode('utf-8'))
except Exception:
    sys.exit(3)
PY_RUNNER_LIVE_COMPANION
}

runner_live_read_manifest() {
    # Parse + validate the release manifest with python json (never eval/source).
    # Prints validated KEY=VALUE lines; any violation exits 3 with no output.
    python3 - "$1" <<'PY_RUNNER_LIVE_MANIFEST'
import json
import os
from pathlib import Path
import re
import stat
import sys

HEX40 = re.compile(r'[0-9a-f]{40}')
HEX64 = re.compile(r'[0-9a-f]{64}')
STAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})')
APPROVER = re.compile(r'[A-Za-z0-9._@:+ -]{1,64}')
FIELDS = ('schema', 'release_sha', 'expected_old_sha', 'companion_sha256',
          'controller_sha256', 'created_at', 'approved_by')


def reject_constant(name):
    raise ValueError('non-finite number')


def unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate key')
        out[key] = value
    return out


try:
    path = Path(sys.argv[1])
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute path required')
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0:
            raise ValueError('untrusted ownership/path')
        sticky_parent = item != path and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        if info.st_mode & 0o022 and not sticky_parent:
            raise ValueError('writable path')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o7777 & ~0o644):
            raise ValueError('untrusted file')
        data = stream.read(65_537)
    if len(data) > 65_536:
        raise ValueError('too large')
    doc = json.loads(data.decode('utf-8'), parse_constant=reject_constant,
                     object_pairs_hook=unique_pairs)
    if not isinstance(doc, dict) or set(doc) != set(FIELDS):
        raise ValueError('field set')
    if type(doc['schema']) is not int or doc['schema'] != 1:
        raise ValueError('schema')
    for key, rx in (('release_sha', HEX40), ('expected_old_sha', HEX40),
                    ('companion_sha256', HEX64), ('controller_sha256', HEX64),
                    ('created_at', STAMP), ('approved_by', APPROVER)):
        if not isinstance(doc[key], str) or not rx.fullmatch(doc[key]):
            raise ValueError(key)
    if doc['release_sha'] == doc['expected_old_sha']:
        raise ValueError('release equals old')
    sys.stdout.write(''.join(f'{key}={doc[key]}\n' for key in FIELDS))
except Exception:
    sys.exit(3)
PY_RUNNER_LIVE_MANIFEST
}

runner_live_install_atomic() {
    # temp file in the destination directory -> fsync -> rename -> fsync dir.
    # Prints the sha256 of the installed bytes.
    python3 - "$1" "$2" <<'PY_RUNNER_LIVE_INSTALL'
import hashlib
import os
import sys

src, dest = sys.argv[1], sys.argv[2]
tmp = None
try:
    if not os.path.isabs(dest) or os.path.islink(dest):
        raise ValueError('bad destination')
    with open(src, 'rb') as stream:
        data = stream.read()
    directory = os.path.dirname(dest)
    fd, tmp = __import__('tempfile').mkstemp(dir=directory, prefix='.runner_live.')
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fchmod(fd, 0o644)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, dest)
    tmp = None
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    print(hashlib.sha256(data).hexdigest())
except Exception:
    if tmp:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    sys.exit(3)
PY_RUNNER_LIVE_INSTALL
}

runner_live_pid_source_ok() {
    # <proc_root> <pid> <live_dir> <release_sha>: the process executes a script
    # that lives in runner-live and whose bytes equal that script at <sha>.
    # unit WorkingDirectory is not runner-live, so cwd is deliberately unused.
    python3 - "$@" <<'PY_RUNNER_LIVE_PID'
import os
import subprocess
import sys

proc_root, pid, live, sha = sys.argv[1:5]
try:
    if not pid.isdigit() or int(pid) < 2:
        raise ValueError('pid')
    live = os.path.realpath(live)
    base = os.path.join(proc_root, pid)
    handle = os.path.join(base, 'fd', '255')
    source = None
    try:
        source = os.readlink(handle)
    except OSError:
        handle = None
    if not source or not source.startswith('/'):
        handle = None
        with open(os.path.join(base, 'cmdline'), 'rb') as stream:
            args = [os.fsdecode(a) for a in stream.read().split(b'\0')[1:] if a.startswith(b'/')]
        if not args:
            raise ValueError('no source')
        source = args[0]
    if source.endswith(' (deleted)'):
        raise ValueError('deleted source')
    real = os.path.realpath(source)
    rel = os.path.relpath(real, live)
    if rel.startswith('..') or os.path.isabs(rel):
        raise ValueError('source outside runner-live')
    with open(handle or real, 'rb') as stream:
        actual = stream.read()
    expected = subprocess.run(['git', '-C', live, 'show', f'{sha}:{rel}'],
                              capture_output=True, timeout=20, check=True).stdout
    if actual != expected:
        raise ValueError('source bytes differ from release')
except Exception:
    sys.exit(3)
PY_RUNNER_LIVE_PID
}

runner_live_git() {
    local live_dir="$1"
    shift
    git -C "$live_dir" "$@"
}

# ── injectable steps (fixtures redefine these; production bodies below) ──────
runner_live_fetch() {
    timeout 60 git -C "$1" fetch --quiet origin main >/dev/null 2>&1
}

runner_live_service_pid() {
    local pid
    pid=$(systemctl show -p MainPID --value "$1" 2>/dev/null) || pid=""
    printf '%s' "${pid//[[:space:]]/}"
}

runner_live_restart_service() {
    # <helper_dir> <service>: only path that restarts the runner. Must be called
    # while BOTH leases are held; --lease-held makes the helper skip its own
    # transaction (a second EX on the same lock would deadlock). The helper dir
    # holds a release copy of restart_local_runner.sh plus a pinned companion, so
    # even a rollback to a SHA without --lease-held uses a script that has it.
    # Exit 3 = deferred because of running work / non-AWARE actor.
    timeout 90 bash "$1/restart_local_runner.sh" --service "$2" --lease-held >/dev/null 2>&1
}

runner_live_actor_running() {
    # <protocol_root> <pid>: exit 0 only if the actor record says phase=RUNNING, belongs
    # to this very process (boot id and start ticks) and carries a source_sha256 equal
    # to the sha256 of the file it runs.
    python3 - "$1" "$2" <<'PY_RUNNER_LIVE_RUNNING'
import hashlib
import json
from pathlib import Path
import sys

root, pid = Path(sys.argv[1]), sys.argv[2]
try:
    if not pid.isdigit() or int(pid) < 2:
        raise ValueError('pid')
    actor = json.loads((root / 'actors' / (pid + '.json')).read_text())
    if actor.get('protocol') != 1 or actor.get('pid') != int(pid) or actor.get('phase') != 'RUNNING':
        raise ValueError('not RUNNING')
    proc = Path('/proc')
    fields = (proc / pid / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] in ('Z', 'X') or actor.get('start_ticks') != fields[19]:
        raise ValueError('process identity')
    if actor.get('boot_id') != (proc / 'sys/kernel/random/boot_id').read_text().strip():
        raise ValueError('boot id')
    digest = hashlib.sha256(Path(actor['source']).read_bytes()).hexdigest()
    if actor.get('source_sha256') != digest:
        raise ValueError('source hash')
except Exception:
    sys.exit(3)
PY_RUNNER_LIVE_RUNNING
}

runner_live_wait_daemon_running() {
    # <protocol_root> <pid> <wait_seconds>: polls runner_live_actor_running to the deadline.
    local root="$1" pid="$2" wait="$3" deadline
    deadline=$((SECONDS + wait))
    while :; do
        runner_live_actor_running "$root" "$pid" && return 0
        (( SECONDS >= deadline )) && return 1
        sleep 0.5
    done
}

runner_live_host_name() {
    local name
    name=$(systemctl show -p Environment --value "$1" 2>/dev/null \
             | tr ' ' '\n' | sed -n 's/^AADS_RUNNER_HOST_NAME=//p' | head -1) || name=""
    name="${name//[[:space:]]/}"
    [[ -n "$name" ]] || name=$(hostname -s 2>/dev/null || true)
    printf '%s' "${name//[[:space:]]/}"
}

runner_live_busy_state() {
    # Needs the verified companion loaded. Prints IDLE only for "no active work":
    # live process probe IDLE AND DB active job count 0 (unknown => not idle).
    local service="$1" probe state count service_state
    probe=$(runner_live_process_probe "$service") || probe=UNKNOWN
    state="${probe%%$'\n'*}"
    if [[ "$state" != IDLE ]]; then
        printf '%s' "${state:-UNKNOWN}"
        return 0
    fi
    count=$(db_active_job_count "$(runner_live_host_name "$service")")
    service_state=$(systemctl is-active "$service" 2>/dev/null || true)
    if should_defer_for_busy "$count" 0 "${service_state//[[:space:]]/}"; then
        printf 'DB_BUSY_OR_UNKNOWN'
    else
        printf 'IDLE'
    fi
}

# ── steps that run while BOTH maintenance leases are held ────────────────────
runner_live_restore_locked() {
    # <live_dir> <old_sha> <companion> <backup_dir> <old_companion_sha>
    local live_dir="$1" old_sha="$2" companion="$3" backup="$4" old_companion_sha="$5"
    runner_live_git "$live_dir" checkout --quiet --force --detach "$old_sha" >/dev/null 2>&1 || return 1
    [[ "$(runner_live_git "$live_dir" rev-parse HEAD 2>/dev/null)" == "$old_sha" ]] || return 1
    if [[ -f "$backup/companion.prev" ]]; then
        [[ "$(runner_live_install_atomic "$backup/companion.prev" "$companion")" == "$old_companion_sha" ]] || return 1
    fi
    return 0
}

runner_live_stage_restart_helpers() {
    # <live_dir> <release_sha> <backup>: restart helper copies. -new pairs the
    # release script with the release companion; -old pairs it with the installed
    # companion (for a rollback restart). Both are private 0700 copies.
    local live_dir="$1" release_sha="$2" backup="$3" dir
    for dir in restart-new restart-old; do
        mkdir -p -- "$backup/$dir" || return 1
        runner_live_git "$live_dir" show "$release_sha:scripts/restart_local_runner.sh" \
            >"$backup/$dir/restart_local_runner.sh" 2>/dev/null || return 1
    done
    cp -- "$backup/companion.new" "$backup/restart-new/runner_busy_lib.sh" || return 1
    cp -- "$backup/companion.prev" "$backup/restart-old/runner_busy_lib.sh" || return 1
}

runner_live_restart_verified() {
    # <helper_dir> <service> <prev_pid> <proc_root> <live_dir> <expect_sha>: restart
    # under the held leases and check the new MainPID runs <expect_sha>'s script.
    # Prints the new pid on success, or "!REASON" on failure (stdout is the only channel).
    local helper="$1" service="$2" prev="$3" proc_root="$4" live_dir="$5" expect="$6" pid
    if ! runner_live_restart_service "$helper" "$service"; then
        printf '!RESTART_FAILED'
    elif ! pid=$(runner_live_wait_new_pid "$service" "$prev"); then
        printf '!PID_UNCHANGED'
    elif [[ "$(runner_live_git "$live_dir" rev-parse HEAD 2>/dev/null)" != "$expect" ]]; then
        printf '!HEAD_NOT_RELEASE_AFTER_RESTART'
    elif ! runner_live_pid_source_ok "$proc_root" "$pid" "$live_dir" "$expect"; then
        printf '!NEW_PID_SOURCE_MISMATCH'
    else
        printf '%s' "$pid"
    fi
}

runner_live_apply_locked() {
    # <live_dir> <old_sha> <release_sha> <companion> <companion_sha> <backup> <old_companion_sha> <service> <proc_root>
    # Runs entirely under both leases: swap -> restart -> new MainPID check.
    # return: 0 applied | 11 changed under lease (nothing touched) | 12..15 swap failed and
    # restored (no restart happened) | 20 restart/PID check failed, files restored and the
    # daemon restarted on the old SHA | 19 failed and NOT restored (reason in $backup/reason).
    local live_dir="$1" old_sha="$2" release_sha="$3" companion="$4" companion_sha="$5"
    local backup="$6" old_companion_sha="$7" service="$8" proc_root="$9" installed
    [[ "$(runner_live_git "$live_dir" rev-parse HEAD 2>/dev/null)" == "$old_sha" ]] || return 11
    [[ -z "$(runner_live_git "$live_dir" --no-optional-locks status --porcelain 2>/dev/null)" ]] || return 11
    runner_live_git "$live_dir" show "$release_sha:scripts/runner_busy_lib.sh" >"$backup/companion.new" 2>/dev/null || return 11
    cat -- "$companion" >"$backup/companion.prev" 2>/dev/null || return 11
    runner_live_stage_restart_helpers "$live_dir" "$release_sha" "$backup" || return 11
    local prev_pid failure=0
    prev_pid=$(runner_live_service_pid "$service")
    printf '%s' "$prev_pid" >"$backup/prev_pid"
    if ! runner_live_git "$live_dir" checkout --quiet --detach "$release_sha" >/dev/null 2>&1; then
        failure=12
    elif [[ "$(runner_live_git "$live_dir" rev-parse HEAD 2>/dev/null)" != "$release_sha" ]]; then
        failure=13
    elif ! installed=$(runner_live_install_atomic "$backup/companion.new" "$companion"); then
        failure=14
    elif [[ "$installed" != "$companion_sha" || "$(sha256sum -- "$companion" 2>/dev/null | cut -d' ' -f1)" != "$companion_sha" ]]; then
        failure=15
    fi
    if (( failure )); then
        runner_live_restore_locked "$live_dir" "$old_sha" "$companion" "$backup" "$old_companion_sha" || return 19
        return "$failure"
    fi

    local result
    result=$(runner_live_restart_verified "$backup/restart-new" "$service" "$prev_pid" "$proc_root" "$live_dir" "$release_sha")
    if [[ "$result" != '!'* ]]; then
        printf '%s' "$result" >"$backup/new_pid"
        return 0
    fi
    printf '%s' "${result#!}" >"$backup/reason"

    # Same lease: restore files, and restart only if the service really restarted
    # (a deferred restart leaves the old process in place).
    runner_live_restore_locked "$live_dir" "$old_sha" "$companion" "$backup" "$old_companion_sha" || return 19
    local after_pid restored
    after_pid=$(runner_live_service_pid "$service")
    if [[ "$after_pid" != "$prev_pid" ]]; then
        restored=$(runner_live_restart_verified "$backup/restart-old" "$service" "$after_pid" "$proc_root" "$live_dir" "$old_sha")
        if [[ "$restored" == '!'* ]]; then
            printf 'restart=unverified' >"$backup/incomplete"
            return 19
        fi
        printf '%s' "$restored" >"$backup/restored_pid"
    fi
    return 20
}

runner_live_rollback_locked() {
    # <live_dir> <old_sha> <companion> <backup> <old_companion_sha> <service> <proc_root>
    # Second transaction (new daemon never reached RUNNING): old SHA, old companion
    # and the daemon restart all happen under one lease. return 0 | 1 | 2 (restart unverified)
    local live_dir="$1" old_sha="$2" companion="$3" backup="$4" old_companion_sha="$5"
    local service="$6" proc_root="$7" current restored
    runner_live_restore_locked "$live_dir" "$old_sha" "$companion" "$backup" "$old_companion_sha" || return 1
    current=$(runner_live_service_pid "$service")
    restored=$(runner_live_restart_verified "$backup/restart-old" "$service" "$current" "$proc_root" "$live_dir" "$old_sha")
    [[ "$restored" != '!'* ]] || return 2
    printf '%s' "$restored" >"$backup/restored_pid"
}

runner_live_wait_new_pid() {
    # <service> <prev_pid>: prints the first MainPID that differs from prev_pid.
    local service="$1" prev="$2" waited pid=""
    for ((waited = 0; waited < RUNNER_LIVE_PID_WAIT; waited++)); do
        pid=$(runner_live_service_pid "$service")
        if [[ -n "$pid" && "$pid" != 0 && "$pid" != "$prev" ]]; then
            printf '%s' "$pid"
            return 0
        fi
        sleep 1
    done
    return 1
}

runner_live_consume_manifest() {
    # <manifest> <applied|rolledback> <release_sha>: rename so it cannot re-fire.
    local manifest="$1" tag="$2" sha="$3"
    mv -T -- "$manifest" "${manifest%.json}.${tag}.${sha}.json" 2>/dev/null
}

# Body of the apply flow. Runs in a subshell with the verified companion loaded.
runner_live_release_flow_inner() (
    local verified="$1" manifest="$2" root="$3" service="$4" proc_root="$5" cgroup_root="$6"
    local companion="$7" live_dir="$8" backup="$9"
    local fields key value schema="" release_sha="" old_sha="" companion_sha="" controller_sha=""
    local created_at="" approved_by="" current pin ok

    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_COMMON_DIR
    if ! fields=$(runner_live_read_manifest "$manifest"); then
        runner_live_defer RELEASE_MANIFEST_INVALID
        return 0
    fi
    while IFS== read -r key value; do
        case "$key" in
            schema) schema="$value" ;;
            release_sha) release_sha="$value" ;;
            expected_old_sha) old_sha="$value" ;;
            companion_sha256) companion_sha="$value" ;;
            controller_sha256) controller_sha="$value" ;;
            created_at) created_at="$value" ;;
            approved_by) approved_by="$value" ;;
        esac
    done <<<"$fields"
    : "$schema" "$created_at" "$approved_by"

    current=$(sha256sum -- "$RUNNER_LIVE_CONTROLLER_PATH" 2>/dev/null | cut -d' ' -f1)
    if [[ "$current" != "$controller_sha" ]]; then
        runner_live_defer CONTROLLER_SHA_MISMATCH
        return 0
    fi
    ok=0
    for pin in $RUNNER_LIVE_COMPANION_PINS; do
        [[ "$pin" == "$companion_sha" ]] && ok=1
    done
    if (( ! ok )); then
        runner_live_defer COMPANION_PIN_MISMATCH
        return 0
    fi

    if ! runner_live_git "$live_dir" rev-parse --git-dir >/dev/null 2>&1; then
        runner_live_defer LIVE_CHECKOUT_UNREADABLE
        return 0
    fi
    runner_live_fetch "$live_dir" || true
    if ! runner_live_git "$live_dir" cat-file -e "$release_sha^{commit}" 2>/dev/null \
       || ! runner_live_git "$live_dir" merge-base --is-ancestor "$release_sha" origin/main 2>/dev/null; then
        runner_live_defer RELEASE_SHA_NOT_IN_MAIN "release=$release_sha"
        return 0
    fi
    current=$(runner_live_git "$live_dir" rev-parse HEAD 2>/dev/null) || current=""
    if [[ "$current" != "$old_sha" ]]; then
        runner_live_defer OLD_SHA_MISMATCH "current=${current:-unknown}" "expected=$old_sha"
        return 0
    fi
    if ! current=$(runner_live_git "$live_dir" --no-optional-locks status --porcelain 2>/dev/null) || [[ -n "$current" ]]; then
        runner_live_defer DIRTY_TREE
        return 0
    fi
    current=$(runner_live_git "$live_dir" show "$release_sha:scripts/runner_busy_lib.sh" 2>/dev/null | sha256sum | cut -d' ' -f1)
    if [[ "$current" != "$companion_sha" ]]; then
        runner_live_defer COMPANION_SHA_MISMATCH
        return 0
    fi
    local old_companion_sha
    old_companion_sha=$(sha256sum -- "$companion" 2>/dev/null | cut -d' ' -f1)

    eval "$verified"
    eval "$(declare -f runner_maintenance_metadata | sed '1s/runner_maintenance_metadata/runner_live_metadata_actual/')"
    runner_maintenance_metadata() {
        case "$1" in
            preflight|quiescent) runner_live_metadata_actual "$@" "$proc_root" "$cgroup_root" ;;
            *) runner_live_metadata_actual "$@" ;;
        esac
    }

    local busy
    busy=$(runner_live_busy_state "$service")
    if [[ "$busy" != IDLE ]]; then
        runner_live_defer BUSY "state=$busy"
        return 0
    fi

    local rc prev_pid new_pid reason
    runner_maintenance_transaction "$service" "$RUNNER_LIVE_LEASE_WAIT" "$root" runner_live_apply_locked \
        "$live_dir" "$old_sha" "$release_sha" "$companion" "$companion_sha" "$backup" "$old_companion_sha" \
        "$service" "$proc_root"
    rc=$?
    prev_pid=$(cat -- "$backup/prev_pid" 2>/dev/null)
    reason=$(cat -- "$backup/reason" 2>/dev/null)
    case "$rc" in
        0) ;;
        3) runner_live_defer LEASE_UNAVAILABLE; return 0 ;;
        11) runner_live_defer CHANGED_UNDER_LEASE; return 0 ;;
        12|13|14|15)
            local why=(x CHECKOUT_FAILED HEAD_NOT_RELEASE COMPANION_INSTALL_FAILED COMPANION_SHA_MISMATCH)
            printf 'ROLLED_BACK runner-live-update reason=%s release=%s restored=%s\n' \
                "${why[rc-11]}" "$release_sha" "$old_sha"
            runner_live_consume_manifest "$manifest" rolledback "$release_sha"
            return 1
            ;;
        20)
            printf 'ROLLED_BACK runner-live-update reason=%s release=%s restored=%s\n' \
                "$reason" "$release_sha" "$old_sha"
            runner_live_consume_manifest "$manifest" rolledback "$release_sha"
            return 1
            ;;
        19)
            printf 'ROLLBACK_INCOMPLETE runner-live-update reason=%s release=%s expected_old=%s %s\n' \
                "${reason:-APPLY_LOCKED_RC_19}" "$release_sha" "$old_sha" \
                "$(cat -- "$backup/incomplete" 2>/dev/null || printf 'restore=failed')"
            return 4
            ;;
        *)
            printf 'ROLLBACK_INCOMPLETE runner-live-update reason=APPLY_LOCKED_RC_%s release=%s expected_old=%s\n' \
                "$rc" "$release_sha" "$old_sha"
            return 4
            ;;
    esac

    # Leases are released. The new daemon (blocked in the admission flock until now)
    # must reach RUNNING with the expected source; its record is the proof.
    new_pid=$(cat -- "$backup/new_pid" 2>/dev/null)
    if runner_live_wait_daemon_running "$root" "$new_pid" "$RUNNER_LIVE_RUNNING_WAIT"; then
        local consumed=consumed
        runner_live_consume_manifest "$manifest" applied "$release_sha" || consumed=consume_failed
        printf 'APPLIED runner-live-update release=%s old=%s prev_pid=%s pid=%s manifest=%s\n' \
            "$release_sha" "$old_sha" "${prev_pid:-none}" "$new_pid" "$consumed"
        [[ "$consumed" == consumed ]] || return 5
        return 0
    fi

    local failure=NEW_DAEMON_NOT_RUNNING
    rm -f -- "$backup/restored_pid"
    runner_maintenance_transaction "$service" "$RUNNER_LIVE_ROLLBACK_LEASE_WAIT" "$root" runner_live_rollback_locked \
        "$live_dir" "$old_sha" "$companion" "$backup" "$old_companion_sha" "$service" "$proc_root"
    rc=$?
    if (( rc )); then
        printf 'ROLLBACK_INCOMPLETE runner-live-update reason=%s release=%s expected_old=%s restore_rc=%s\n' \
            "$failure" "$release_sha" "$old_sha" "$rc"
        return 4
    fi
    local restored_pid
    restored_pid=$(cat -- "$backup/restored_pid" 2>/dev/null)
    if ! runner_live_wait_daemon_running "$root" "$restored_pid" "$RUNNER_LIVE_RUNNING_WAIT"; then
        printf 'ROLLBACK_INCOMPLETE runner-live-update reason=%s release=%s expected_old=%s restart=unverified\n' \
            "$failure" "$release_sha" "$old_sha"
        return 4
    fi
    printf 'ROLLED_BACK runner-live-update reason=%s release=%s restored=%s\n' \
        "$failure" "$release_sha" "$old_sha"
    runner_live_consume_manifest "$manifest" rolledback "$release_sha"
    return 1
)

runner_live_release_flow() {
    local backup rc
    backup=$(mktemp -d "${TMPDIR:-/tmp}/runner-live-release.XXXXXX") || {
        runner_live_defer BACKUP_DIR_UNAVAILABLE
        return 0
    }
    chmod 0700 "$backup"
    runner_live_release_flow_inner "$@" "$backup"
    rc=$?
    rm -rf -- "$backup"
    return "$rc"
}

runner_live_update_gate() {
    # Arguments allow isolated real-process fixtures; the executable entrypoint
    # below pins all production paths and does not accept environment overrides.
    local companion="$1" release_manifest="$2" protocol_root="$3" service="$4"
    local proc_root="${5:-/proc}" cgroup_root="${6:-/sys/fs/cgroup}"
    local live_dir="${7:-/root/aads/runner-live}"
    local verified state rc
    if ! verified=$(runner_live_verified_companion "$companion"); then
        runner_live_defer COMPANION_MISSING_OR_UNTRUSTED
        return 0
    fi
    # Diagnostic only: metadata preflight performs inventory reads. No init,
    # record, admission/lifetime lock acquisition or apply callback is invoked.
    if state=$(timeout 15 bash -c "$verified"$'\n''runner_maintenance_metadata preflight "$@"' \
            runner-live-preflight "$protocol_root" "$service" "$proc_root" "$cgroup_root" 2>/dev/null); then
        rc=0
    else
        rc=$?
    fi
    if [[ "$state" == BOOTSTRAP_REQUIRED ]]; then
        runner_live_defer BOOTSTRAP_REQUIRED
    elif [[ "$rc" != 0 || "$state" != AWARE ]]; then
        runner_live_defer PREFLIGHT_UNKNOWN
    elif [[ ! -f "$release_manifest" ]]; then
        runner_live_defer RELEASE_MANIFEST_MISSING_AUTO_APPLY_DISABLED
    else
        runner_live_release_flow "$verified" "$release_manifest" "$protocol_root" "$service" \
            "$proc_root" "$cgroup_root" "$companion" "$live_dir"
        return $?
    fi
    return 0
}

runner_live_update_main() {
    export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    if (( $# )); then
        runner_live_defer UNSUPPORTED_ARGUMENTS_AUTO_APPLY_DISABLED
        return 0
    fi
    runner_live_update_gate \
        /usr/local/lib/aads-runner/runner_busy_lib.sh \
        /etc/aads/runner-live-release.json \
        /run/aads-runner-maintenance \
        aads-pipeline-runner.service \
        /proc /sys/fs/cgroup \
        /root/aads/runner-live
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    runner_live_update_main "$@"
fi
