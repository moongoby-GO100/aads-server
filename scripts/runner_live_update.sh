#!/usr/bin/env bash
# Fail-closed replacement for the installed origin/main checkout updater.
#
# This is a controller HOLD release. It NEVER fetches, checks out, installs,
# signals, or restarts a Runner, including when preflight reports AWARE and a
# release manifest exists. A positive apply path is deliberately absent until
# exact release/old-SHA validation, dirty-tree checks, both maintenance leases,
# and new-PID/source/helper verification have their own reviewed implementation.
# A manifest is not consumed or accepted by this release.
#
# Install only from a committed/published source with expected-old checksum CAS
# and an atomic controller replacement. Installing this controller does not
# enroll a legacy Runner or establish a safe legacy bootstrap procedure.
set -uo pipefail

runner_live_defer() {
    printf 'DEFER runner-live-update reason=%s\n' "$1"
    return 0
}

runner_live_verified_companion() {
    # Return the verified bytes, not a path that could be replaced between a
    # checksum check and source. Never execute untrusted/unpinned companion code.
    python3 - "$1" <<'PY_RUNNER_LIVE_COMPANION'
import hashlib
import os
from pathlib import Path
import stat
import sys

expected = '845f029bcfab4421b7c9189214a3ff15882cccb517608f91e14ee9e160c73282'
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
        if len(data) > 1_048_576 or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('unpinned companion')
    sys.stdout.write(data.decode('utf-8'))
except Exception:
    sys.exit(3)
PY_RUNNER_LIVE_COMPANION
}

runner_live_update_gate() {
    # Arguments allow isolated real-process fixtures; the executable entrypoint
    # below pins all production paths and does not accept environment overrides.
    local companion="$1" release_manifest="$2" protocol_root="$3" service="$4"
    local proc_root="${5:-/proc}" cgroup_root="${6:-/sys/fs/cgroup}"
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
        # Even well-formed, root-owned release metadata is NOT authorization in
        # this HOLD-only implementation. Never parse/eval it or follow an apply.
        runner_live_defer RELEASE_MANIFEST_NOT_ACCEPTED_AUTO_APPLY_DISABLED
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
        aads-pipeline-runner.service
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    runner_live_update_main "$@"
fi
