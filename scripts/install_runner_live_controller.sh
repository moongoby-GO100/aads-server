#!/usr/bin/env bash
# Install the runner-live controller + companion from a committed release blob.
#
# usage: install_runner_live_controller.sh --release-sha <40hex>
#            --expect-controller-sha256 <64hex> --expect-companion-sha256 <64hex> [--dry-run]
#
# The --expect-* values are the sha256 of the files installed RIGHT NOW (CAS). Any
# difference, an unreachable release, an unpinned companion or a lease that cannot
# be taken prints DEFER and writes nothing. Sources are only the commit blobs
# `git show <release_sha>:scripts/runner_live_update.sh` / `:scripts/runner_busy_lib.sh`
# of the runner-live checkout; release_sha must be an ancestor of origin/main.
# The swap runs inside the existing runner_maintenance_transaction (no new lock),
# companion first, controller second; a failed controller swap restores the companion.
# Production paths are fixed below; tests inject paths only as function arguments.
#
# Design / procedure: docs/design/20261014_AADS_러너라이브_정식반영경로_설계.md (§5)
set -uo pipefail

INSTALLER_SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")"
INSTALLER_LEASE_WAIT=20
INSTALLER_CONTROLLER_REL=scripts/runner_live_update.sh
INSTALLER_COMPANION_REL=scripts/runner_busy_lib.sh

installer_log() {
    # <KIND> [key=value ...]: one structured stdout line.
    local kind="$1"
    shift
    printf '%s runner-live-install' "$kind"
    (( $# )) && printf ' %s' "$@"
    printf '\n'
}

installer_defer() {
    local reason="$1"
    shift
    installer_log DEFER "reason=$reason" "$@"
    return 0
}

installer_sha_of() {
    # sha256 of a regular, non-symlink file; "missing" otherwise.
    local path="$1" digest
    if [[ -f "$path" && ! -L "$path" ]] && digest=$(sha256sum -- "$path" 2>/dev/null); then
        printf '%s' "${digest%% *}"
    else
        printf 'missing'
    fi
}

installer_install_atomic() {
    # <src> <dest> <mode-octal>: same-directory temp -> root:root -> mode -> fsync -> rename -> fsync dir.
    # Prints the sha256 of the installed bytes; exit 3 on any failure (dest untouched).
    python3 - "$1" "$2" "$3" <<'PY_INSTALLER_ATOMIC'
import hashlib
import os
import sys
import tempfile

src, dest, mode = sys.argv[1], sys.argv[2], int(sys.argv[3], 8)
tmp = None
try:
    if not os.path.isabs(dest) or os.path.islink(dest):
        raise ValueError('bad destination')
    with open(src, 'rb') as stream:
        data = stream.read()
    directory = os.path.dirname(dest)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.runner_live_install.')
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fchown(fd, 0, 0)
        os.fchmod(fd, mode)
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
PY_INSTALLER_ATOMIC
}

installer_verified_companion() {
    # <path> <pinned-sha256>: prints the bytes only if the root-owned, non-writable,
    # non-symlink file hashes to the pin (never run unverified companion code).
    python3 - "$1" "$2" <<'PY_INSTALLER_COMPANION'
import hashlib
import os
from pathlib import Path
import stat
import sys

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
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('untrusted file')
        data = stream.read(1_048_577)
        if len(data) > 1_048_576 or hashlib.sha256(data).hexdigest() != sys.argv[2]:
            raise ValueError('unpinned companion')
    sys.stdout.write(data.decode('utf-8'))
except Exception:
    sys.exit(3)
PY_INSTALLER_COMPANION
}

installer_fetch() {
    timeout 60 git -C "$1" fetch --quiet origin main >/dev/null 2>&1
}

installer_git() {
    local live_dir="$1"
    shift
    git -C "$live_dir" "$@"
}

installer_pins_of() {
    # Extract the RUNNER_LIVE_COMPANION_PINS list from a controller blob without
    # sourcing or evaluating it. Prints nothing unless the line is a pure hex list.
    sed -n "s/^RUNNER_LIVE_COMPANION_PINS='\([0-9a-f ]*\)'\$/\1/p" -- "$1" | head -n1
}

installer_restore_locked() {
    # <work> <controller> <companion>: put the previous bytes back (companion first is
    # irrelevant here; both come from the private copies taken under the lease).
    local work="$1" controller="$2" companion="$3" ok=0
    [[ "$(installer_install_atomic "$work/companion.prev" "$companion" 644)" == "$(installer_sha_of "$work/companion.prev")" ]] || ok=1
    if [[ "${4:-both}" == both ]]; then
        [[ "$(installer_install_atomic "$work/controller.prev" "$controller" 755)" == "$(installer_sha_of "$work/controller.prev")" ]] || ok=1
    fi
    return "$ok"
}

installer_install_locked() {
    # <controller> <companion> <work> <expect_ctrl> <expect_comp> <new_ctrl_sha> <new_comp_sha>
    # Runs with BOTH maintenance leases held. return: 0 installed | 11 installed files changed
    # under the lease (nothing written) | 15 backup failed (nothing written) | 12 companion
    # install failed, restored | 13 controller install failed, companion restored | 14 post-install
    # checksum mismatch, both restored | 19 restore failed.
    local controller="$1" companion="$2" work="$3" exp_ctrl="$4" exp_comp="$5" new_ctrl="$6" new_comp="$7"
    local old_ctrl16="${exp_ctrl:0:16}" old_comp16="${exp_comp:0:16}" installed

    cat -- "$controller" >"$work/controller.prev" 2>/dev/null || return 11
    cat -- "$companion" >"$work/companion.prev" 2>/dev/null || return 11
    [[ "$(installer_sha_of "$work/controller.prev")" == "$exp_ctrl" \
        && "$(installer_sha_of "$work/companion.prev")" == "$exp_comp" ]] || return 11

    [[ "$(installer_install_atomic "$work/controller.prev" "$controller.prev.$old_ctrl16" 755)" == "$exp_ctrl" ]] || return 15
    [[ "$(installer_install_atomic "$work/companion.prev" "$companion.prev.$old_comp16" 644)" == "$exp_comp" ]] || return 15

    if ! installed=$(installer_install_atomic "$work/companion.new" "$companion" 644) || [[ "$installed" != "$new_comp" ]]; then
        installer_restore_locked "$work" "$controller" "$companion" companion || return 19
        return 12
    fi
    if ! installed=$(installer_install_atomic "$work/controller.new" "$controller" 755) || [[ "$installed" != "$new_ctrl" ]]; then
        installer_restore_locked "$work" "$controller" "$companion" companion || return 19
        return 13
    fi
    if [[ "$(installer_sha_of "$controller")" != "$new_ctrl" || "$(installer_sha_of "$companion")" != "$new_comp" ]]; then
        installer_restore_locked "$work" "$controller" "$companion" both || return 19
        return 14
    fi
    return 0
}

installer_print_dry_run() {
    # <controller> <companion> <release> <exp_ctrl> <exp_comp> <new_ctrl> <new_comp>
    local controller="$1" companion="$2" release="$3" exp_ctrl="$4" exp_comp="$5" new_ctrl="$6" new_comp="$7"
    installer_log DRYRUN "release=$release" "controller=$controller" "controller_current=${exp_ctrl:0:16}" \
        "controller_new=${new_ctrl:0:16}" "companion=$companion" "companion_current=${exp_comp:0:16}" \
        "companion_new=${new_comp:0:16}" "pin=ok" "preflight=AWARE" "writes=none"
    printf 'INSTALL_CMD: bash %s --release-sha %s --expect-controller-sha256 %s --expect-companion-sha256 %s\n' \
        "$INSTALLER_SCRIPT_PATH" "$release" "$exp_ctrl" "$exp_comp"
    printf 'ROLLBACK_CMD: install -o root -g root -m 0644 %s %s/.runner_busy_lib.sh.rollback && mv -T %s/.runner_busy_lib.sh.rollback %s\n' \
        "$companion.prev.${exp_comp:0:16}" "$(dirname -- "$companion")" "$(dirname -- "$companion")" "$companion"
    printf 'ROLLBACK_CMD: install -o root -g root -m 0755 %s %s/.aads-runner-live-update.rollback && mv -T %s/.aads-runner-live-update.rollback %s\n' \
        "$controller.prev.${exp_ctrl:0:16}" "$(dirname -- "$controller")" "$(dirname -- "$controller")" "$controller"
}

# Body runs in a subshell so the sourced companion never leaks into the caller.
installer_run_inner() (
    local controller="$1" companion="$2" root="$3" service="$4" proc_root="$5" cgroup_root="$6" live_dir="$7"
    local release="$8" exp_ctrl="$9" exp_comp="${10}" dry_run="${11}" work="${12}"
    local cur_ctrl cur_comp new_ctrl new_comp pins pin ok=0 verified state rc

    unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_COMMON_DIR

    cur_ctrl=$(installer_sha_of "$controller")
    cur_comp=$(installer_sha_of "$companion")
    if [[ "$cur_ctrl" != "$exp_ctrl" || "$cur_comp" != "$exp_comp" ]]; then
        installer_defer INSTALLED_CHECKSUM_MISMATCH \
            "controller_current=$cur_ctrl" "controller_expected=$exp_ctrl" \
            "companion_current=$cur_comp" "companion_expected=$exp_comp"
        return 0
    fi

    if ! installer_git "$live_dir" rev-parse --git-dir >/dev/null 2>&1; then
        installer_defer LIVE_CHECKOUT_UNREADABLE
        return 0
    fi
    installer_fetch "$live_dir" || true
    if ! installer_git "$live_dir" cat-file -e "$release^{commit}" 2>/dev/null \
       || ! installer_git "$live_dir" merge-base --is-ancestor "$release" origin/main 2>/dev/null; then
        installer_defer RELEASE_SHA_NOT_IN_MAIN "release=$release"
        return 0
    fi

    if ! installer_git "$live_dir" show "$release:$INSTALLER_CONTROLLER_REL" >"$work/controller.new" 2>/dev/null \
       || ! installer_git "$live_dir" show "$release:$INSTALLER_COMPANION_REL" >"$work/companion.new" 2>/dev/null \
       || [[ ! -s "$work/controller.new" || ! -s "$work/companion.new" ]]; then
        installer_defer RELEASE_BLOB_MISSING "release=$release"
        return 0
    fi
    new_ctrl=$(installer_sha_of "$work/controller.new")
    new_comp=$(installer_sha_of "$work/companion.new")
    pins=$(installer_pins_of "$work/controller.new")
    for pin in $pins; do
        [[ "$pin" == "$new_comp" ]] && ok=1
    done
    if (( ! ok )); then
        installer_defer COMPANION_PIN_MISMATCH "companion_new=$new_comp"
        return 0
    fi

    # The lease protocol comes from the installed companion, verified byte-for-byte
    # against the checksum the operator expects (== CAS above).
    if ! verified=$(installer_verified_companion "$companion" "$exp_comp"); then
        installer_defer COMPANION_MISSING_OR_UNTRUSTED
        return 0
    fi
    eval "$verified"
    eval "$(declare -f runner_maintenance_metadata | sed '1s/runner_maintenance_metadata/installer_metadata_actual/')"
    runner_maintenance_metadata() {
        case "$1" in
            preflight|quiescent) installer_metadata_actual "$@" "$proc_root" "$cgroup_root" ;;
            *) installer_metadata_actual "$@" ;;
        esac
    }

    if [[ "$dry_run" == 1 ]]; then
        state=$(timeout 15 bash -c "$verified"$'\n''runner_maintenance_metadata preflight "$@"' \
            runner-live-preflight "$root" "$service" "$proc_root" "$cgroup_root" 2>/dev/null) || state=UNKNOWN
        if [[ "$state" == BOOTSTRAP_REQUIRED ]]; then
            installer_defer BOOTSTRAP_REQUIRED
        elif [[ "$state" != AWARE ]]; then
            installer_defer PREFLIGHT_UNKNOWN
        else
            installer_print_dry_run "$controller" "$companion" "$release" "$exp_ctrl" "$exp_comp" "$new_ctrl" "$new_comp"
        fi
        return 0
    fi

    runner_maintenance_transaction "$service" "$INSTALLER_LEASE_WAIT" "$root" installer_install_locked \
        "$controller" "$companion" "$work" "$exp_ctrl" "$exp_comp" "$new_ctrl" "$new_comp"
    rc=$?
    case "$rc" in
        0)
            installer_log INSTALLED "release=$release" "controller_old=${exp_ctrl:0:16}" "controller_new=${new_ctrl:0:16}" \
                "companion_old=${exp_comp:0:16}" "companion_new=${new_comp:0:16}" \
                "backup_controller=$controller.prev.${exp_ctrl:0:16}" "backup_companion=$companion.prev.${exp_comp:0:16}"
            return 0
            ;;
        3) installer_defer LEASE_UNAVAILABLE ;;
        11) installer_defer CHANGED_UNDER_LEASE ;;
        15) installer_defer BACKUP_FAILED ;;
        12|13|14)
            local why=(x COMPANION_INSTALL_FAILED CONTROLLER_RENAME_FAILED POST_INSTALL_CHECKSUM_MISMATCH)
            installer_log ROLLED_BACK "reason=${why[rc-11]}" "release=$release" \
                "controller_restored=${exp_ctrl:0:16}" "companion_restored=${exp_comp:0:16}"
            return 1
            ;;
        *)
            installer_log ROLLBACK_INCOMPLETE "reason=RESTORE_FAILED_RC_$rc" "release=$release" \
                "controller_expected=$exp_ctrl" "companion_expected=$exp_comp" \
                "controller_now=$(installer_sha_of "$controller")" "companion_now=$(installer_sha_of "$companion")"
            return 4
            ;;
    esac
    return 0
)

installer_usage() {
    echo "usage: $0 --release-sha <40hex> --expect-controller-sha256 <64hex> --expect-companion-sha256 <64hex> [--dry-run]" >&2
}

installer_run() {
    # <controller> <companion> <protocol_root> <service> <proc_root> <cgroup_root> <live_dir> [flags...]
    # Paths come only from these arguments (the production entrypoint fixes them).
    local controller="$1" companion="$2" root="$3" service="$4" proc_root="$5" cgroup_root="$6" live_dir="$7"
    shift 7
    local release="" exp_ctrl="" exp_comp="" dry_run=0
    while (( $# )); do
        case "$1" in
            --release-sha) [[ -z "$release" && $# -ge 2 ]] || { installer_usage; return 2; }; release="$2"; shift 2 ;;
            --expect-controller-sha256) [[ -z "$exp_ctrl" && $# -ge 2 ]] || { installer_usage; return 2; }; exp_ctrl="$2"; shift 2 ;;
            --expect-companion-sha256) [[ -z "$exp_comp" && $# -ge 2 ]] || { installer_usage; return 2; }; exp_comp="$2"; shift 2 ;;
            --dry-run) dry_run=1; shift ;;
            *) installer_usage; return 2 ;;
        esac
    done
    if [[ ! "$release" =~ ^[0-9a-f]{40}$ || ! "$exp_ctrl" =~ ^[0-9a-f]{64}$ || ! "$exp_comp" =~ ^[0-9a-f]{64}$ ]]; then
        installer_usage
        return 2
    fi
    local work rc
    work=$(mktemp -d "${TMPDIR:-/tmp}/runner-live-install.XXXXXX") || {
        installer_defer WORK_DIR_UNAVAILABLE
        return 0
    }
    chmod 0700 "$work"
    installer_run_inner "$controller" "$companion" "$root" "$service" "$proc_root" "$cgroup_root" "$live_dir" \
        "$release" "$exp_ctrl" "$exp_comp" "$dry_run" "$work"
    rc=$?
    rm -rf -- "$work"
    return "$rc"
}

installer_main() {
    export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    installer_run \
        /usr/local/sbin/aads-runner-live-update \
        /usr/local/lib/aads-runner/runner_busy_lib.sh \
        /run/aads-runner-maintenance \
        aads-pipeline-runner.service \
        /proc /sys/fs/cgroup \
        /root/aads/runner-live \
        "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    installer_main "$@"
fi
