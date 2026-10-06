#!/usr/bin/env bash
# ── 러너 busy 판정 공용 규칙 (AADS-RUNNER-SYNC-BUSY-DEFER, 2026-09-16) ────
#
# 러너를 재시작하면 실행 중이던 작업은 runner_shutdown_requeued 로 되돌아가
# 처음부터 다시 돈다. 2026-09-16 10:28:51 KST 에 GO100 runner-1791da41(P0)이
# 6분 16초 지점에서 그렇게 버려졌다. 그래서 "지금 재시작해도 되는가" 를
# 판정하는 규칙은 한 곳에만 둔다 — 원격 동기화와 로컬 재시작이 같은 답을 내야 한다.
#
# 사용처:
#   scripts/sync_pipeline_runner_remote.sh  (원격 3개 호스트)
#   scripts/restart_local_runner.sh         (116 로컬 러너)
#
# 이 파일은 source 전용이다. set -e 를 켜지 않는다(호출자 설정을 존중).

PG_CONTAINER="${PG_CONTAINER:-aads-postgres}"
PGUSER="${PGUSER:-aads}"
PGDATABASE="${PGDATABASE:-aads}"

# DB terminal 상태도 살아 있는 CLI를 증명하지 못한다. 이 함수는 원격에 설치하지
# 않고 SSH stdin으로 전달할 수 있게 stdlib만 사용한다. 불완전한 조회는 UNKNOWN.
# 이것은 관측 guard이며 claim/restart를 직렬화하는 maintenance lease는 아니다.
runner_live_process_probe() {
    python3 - "$1" <<'PY_RUNNER_LIVE_PROCESS'
import os
from pathlib import Path
import re
import subprocess
import sys


def _probe(service, proc_root=Path('/proc'), cgroup_root=Path('/sys/fs/cgroup'), show=None):
    if not re.fullmatch(r'[A-Za-z0-9_.@:-]+', service):
        return 'UNKNOWN'
    if show is None:
        result = subprocess.run(
            ['systemctl', 'show', service, '-p', 'LoadState', '-p', 'ActiveState',
             '-p', 'MainPID', '-p', 'ControlGroup'], capture_output=True, text=True, timeout=10)
        if result.returncode:
            return 'UNKNOWN'
        show = result.stdout
    props = dict(line.split('=', 1) for line in show.splitlines() if '=' in line)
    if props.get('LoadState') != 'loaded' or props.get('ActiveState') not in ('active', 'inactive', 'failed'):
        return 'UNKNOWN'
    if not props.get('MainPID', '').isdigit() or 'ControlGroup' not in props:
        return 'UNKNOWN'
    main_pid = int(props['MainPID'])
    if props['ActiveState'] == 'active' and not main_pid:
        return 'UNKNOWN'
    processes = {}
    for directory in proc_root.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            stat = (directory / 'stat').read_text()
        except FileNotFoundError:
            continue  # A process exited during the read.
        fields = stat[stat.rfind(')') + 2:].split()
        pid = int(directory.name)
        state, ppid, flags = fields[0], int(fields[1]), int(fields[6])
        if state in ('Z', 'X') or flags & 0x00200000:  # exited process or kernel thread
            continue
        try:
            cwd = os.readlink(directory / 'cwd')
        except FileNotFoundError:
            if not (directory / 'stat').exists():
                continue
            return 'UNKNOWN'
        if cwd.endswith(' (deleted)'):
            cwd = cwd[:-len(' (deleted)')]
        if cwd.startswith('/tmp/aads-wt-'):
            return 'BUSY'  # Includes detached/orphaned workers outside the current cgroup.
        processes[pid] = ppid
    group = props['ControlGroup']
    members = set()
    if group:
        relative = Path(group.lstrip('/'))
        if '..' in relative.parts or not group.startswith('/'):
            return 'UNKNOWN'
        directory = cgroup_root / relative
        if not directory.is_dir():
            return 'UNKNOWN'
        def read_group(path):
            # Explicit recursion propagates permission errors; rglob can hide them.
            members.update(int(pid) for pid in (path / 'cgroup.procs').read_text().split())
            for child in path.iterdir():
                if child.is_dir():
                    read_group(child)
        read_group(directory)
    elif main_pid or props['ActiveState'] == 'active':
        return 'UNKNOWN'
    if main_pid and (main_pid not in processes or main_pid not in members):
        return 'UNKNOWN'
    descendants = {main_pid} if main_pid else set()
    while True:
        new = descendants | {pid for pid, parent in processes.items() if parent in descendants}
        if new == descendants:
            break
        descendants = new
    # Conservative: even an idle polling child can defer automatic sync. Only
    # a verified empty worker set permits the existing DB guard to decide.
    if (members | descendants) - {main_pid}:
        return 'BUSY'
    return 'IDLE'


def probe(*args, **kwargs):
    try:
        return _probe(*args, **kwargs)
    except Exception:
        return 'UNKNOWN'


if __name__ == '__main__':
    try:
        print(probe(sys.argv[1]))
    except Exception:
        print('UNKNOWN')
PY_RUNNER_LIVE_PROCESS
}

should_defer_for_processes() {
    [[ "${1:-UNKNOWN}" != "IDLE" ]]
}

# 해당 러너 호스트가 붙잡고 있는 작업 수. 조회 실패/이름 미상이면 빈 문자열.
db_active_job_count() {
    local host_name="$1" out=""
    [[ -n "$host_name" ]] || { printf ''; return 0; }
    [[ "$host_name" =~ ^[A-Za-z0-9._-]+$ ]] || { printf ''; return 0; }
    out=$(docker exec -i "$PG_CONTAINER" psql -U "$PGUSER" -d "$PGDATABASE" \
            -q -t -A -P footer=off \
            -c "SELECT count(*) FROM pipeline_jobs WHERE status IN ('claimed','running','deploying') AND runner_host='${host_name}';" \
            </dev/null 2>/dev/null | tr -d '[:space:]') || out=""
    [[ "$out" =~ ^[0-9]+$ ]] || out=""
    printf '%s' "$out"
}

# 0 = 미루기, 1 = 진행.
# 호출자는 먼저 should_defer_for_processes로 실제 작업자 부재를 확인해야 한다.
# 아래 inactive/ignore_busy 단축은 DB 상태만의 판정이며 프로세스 guard를 대체하지 않는다.
#
# 인자: <busy_count> [ignore_busy] [service_state]
#
# 판정 순서와 근거:
#  1) ignore_busy=1        → 진행. 러너가 멈춰 작업이 영원히 running 인 경우의 탈출구.
#  2) 서비스가 inactive/failed → 진행. 러너가 죽어 있으면 그 호스트의 in-flight 는
#     좀비이고, 재시작이 곧 복구다. 이게 없으면 죽은 러너의 잔존 job 때문에
#     그 호스트는 5분 타이머마다 영원히 defer 된다.
#  3) 작업 1건 이상        → 미룸.
#  4) 판별 불가(빈 값/비정상) → 미룸(fail-closed). 타이머가 5분 뒤 다시 시도하므로
#     비용은 지연뿐이지만, 잘못 진행하면 P0 작업이 처음부터 다시 돈다.
#
# 시간 기반 stale 판정(updated_at 이 N분 안 움직이면 좀비)은 쓰지 않는다.
# 실측 결과 pipeline_jobs.updated_at 은 status='deploying' 구간에서만 갱신되고
# claude_code_work 중에는 움직이지 않는다 — 정상 작업을 좀비로 오판한다.
should_defer_for_busy() {
    local busy_count="$1" ignore_busy="${2:-0}" service_state="${3:-}"
    [[ "$ignore_busy" == "1" ]] && return 1
    [[ "$service_state" == "inactive" || "$service_state" == "failed" ]] && return 1
    [[ "$busy_count" =~ ^[0-9]+$ ]] || return 0
    [[ "$busy_count" -gt 0 ]] && return 0
    return 1
}

# Host-local protocol v1. Admission EX is the drain signal; there is no expiring
# flag or TTL that can release a live worker's lease. FD 200 is inherited by jobs.
runner_maintenance_metadata() {
    python3 - "$@" <<'PY_RUNNER_MAINTENANCE'
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time


class Deferred(Exception):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(pid, proc=Path('/proc')):
    fields = (proc / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    if fields[0] in ('Z', 'X'):
        raise Deferred('exited actor')
    return int(fields[1]), fields[19]


def trusted(path, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022 or (directory and not stat.S_ISDIR(info.st_mode))):
        raise Deferred('untrusted protocol path')
    return info


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(root):
    root.mkdir(mode=0o700, parents=False, exist_ok=True)
    trusted(root, True)
    for name in ('actors', 'enrolled'):
        (root / name).mkdir(mode=0o700, exist_ok=True)
        trusted(root / name, True)
    for name in ('admission.lock', 'lifetime.lock'):
        fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        trusted(root / name)


def record(root, pid, phase, source, library, proc=Path('/proc')):
    if phase not in ('WAIT_ADMISSION', 'RUNNING', 'DRAINING', 'QUIESCENT'):
        raise Deferred('invalid phase')
    _, started = identity(pid, proc)
    boot = (proc / 'sys/kernel/random/boot_id').read_text().strip()
    source, library = Path(source).resolve(strict=True), Path(library).resolve(strict=True)
    trusted(source); trusted(library)
    value = dict(protocol=1, pid=pid, boot_id=boot, start_ticks=started, phase=phase,
                 source=str(source), source_sha256=digest(source), library=str(library),
                 library_sha256=digest(library))
    previous = root / 'actors' / (str(pid) + '.json')
    if previous.exists():
        trusted(previous)
        old = json.loads(previous.read_text())
        if (old.get('pid'), old.get('boot_id'), old.get('start_ticks')) == (pid, boot, started):
            for field in ('source_sha256', 'library_sha256'):
                value[field] = old[field]  # Running code identity never follows later disk replacement.
    atomic_json(previous, value)
    # A running protocol actor can enroll only its actual systemd cgroup. This
    # cannot bootstrap an old daemon or an inactive service that never ran v1.
    if phase == 'RUNNING':
        for line in (proc / str(pid) / 'cgroup').read_text().splitlines():
            services = [p for p in line.split(':', 2)[-1].split('/') if p.endswith('.service')]
            if services:
                service = services[-1]
                if re.fullmatch(r'[A-Za-z0-9_.@:-]+', service):
                    atomic_json(root / 'enrolled' / (service + '.json'), value)


def same_fd(pid, fd, path, proc):
    try:
        actual, expected = (proc / str(pid) / 'fd' / str(fd)).stat(), path.stat()
        return (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
    except FileNotFoundError:
        return False


def read_actor(root, pid, boot, proc):
    path = root / 'actors' / (str(pid) + '.json')
    trusted(path)
    value = json.loads(path.read_text())
    _, started = identity(pid, proc)
    if (value.get('protocol') != 1 or value.get('pid') != pid
            or value.get('boot_id') != boot or value.get('start_ticks') != started):
        raise Deferred('BOOTSTRAP_REQUIRED actor identity mismatch')
    command = (proc / str(pid) / 'cmdline').read_bytes().split(b'\0')
    if os.fsencode(value['source']) not in command:
        raise Deferred('BOOTSTRAP_REQUIRED actor source mismatch')
    if (not re.fullmatch(r'[0-9a-f]{64}', value.get('source_sha256', ''))
            or not re.fullmatch(r'[0-9a-f]{64}', value.get('library_sha256', ''))
            or digest(proc / str(pid) / 'fd' / '255') != value['source_sha256']):
        raise Deferred('BOOTSTRAP_REQUIRED running source fingerprint mismatch')
    phase = value.get('phase')
    fd, name = (200, 'lifetime.lock') if phase in ('RUNNING', 'DRAINING') else (201, 'admission.lock')
    if phase not in ('RUNNING', 'DRAINING', 'WAIT_ADMISSION', 'QUIESCENT') or not same_fd(pid, fd, root / name, proc):
        raise Deferred('BOOTSTRAP_REQUIRED actor descriptor mismatch')
    if phase in ('RUNNING', 'DRAINING'):
        lock_info = (proc / str(pid) / 'fdinfo' / '200').read_text()
        if not re.search(r'FLOCK\s+ADVISORY\s+READ\s', lock_info):
            raise Deferred('BOOTSTRAP_REQUIRED lifetime descriptor is not locked')
    return value


def inventory(root, service, quiescent=False, proc=Path('/proc'), cgroups=Path('/sys/fs/cgroup'), show=None, restart=None):
    if not re.fullmatch(r'[A-Za-z0-9_.@:-]+', service):
        raise Deferred('invalid service')
    trusted(root, True)
    for name in ('admission.lock', 'lifetime.lock'):
        trusted(root / name)
    boot = (proc / 'sys/kernel/random/boot_id').read_text().strip()
    services = sorted({service, 'aads-pipeline-runner.service', 'aads-pipeline-litellm-runner.service'})
    mains, members = {}, set()
    for unit in services:
        if show is None:
            query = subprocess.run(['systemctl', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState',
                                    '-p', 'MainPID', '-p', 'ControlGroup', '-p', 'ExecStart'], capture_output=True, text=True, timeout=3)
            if query.returncode:
                raise Deferred('systemctl query failed')
            value = query.stdout
        else:
            value = show[unit]
        props = dict(line.split('=', 1) for line in value.splitlines() if '=' in line)
        if props.get('LoadState') == 'not-found' and unit != service:
            continue
        if props.get('LoadState') != 'loaded' or props.get('ActiveState') not in ('active', 'inactive', 'failed'):
            raise Deferred('unknown service state')
        if not props.get('MainPID', '').isdigit() or 'ControlGroup' not in props:
            raise Deferred('incomplete service properties')
        pid = int(props['MainPID'])
        if props['ActiveState'] == 'active' and not pid:
            raise Deferred('active service without MainPID')
        if pid:
            actor = read_actor(root, pid, boot, proc)
            if quiescent and actor['phase'] not in ('WAIT_ADMISSION', 'QUIESCENT'):
                raise Deferred('actor not quiescent')
            if restart is not None and unit == service:
                if (pid == restart['previous_pid'] or actor['phase'] != 'WAIT_ADMISSION'
                        or actor['source'] != restart['source']
                        or actor['source_sha256'] != restart['source_sha256']
                        or actor['library_sha256'] != restart['library_sha256']
                        or same_fd(pid, 200, root / 'lifetime.lock', proc)):
                    raise Deferred('restart actor identity/source/admission mismatch')
            mains[pid] = actor
        else:
            if restart is not None and unit == service:
                raise Deferred('restart actor has no MainPID')
            # Inactive is not a bootstrap authorization: it could start between
            # the observation and apply. Its installed startup must already be v1.
            entry = root / 'enrolled' / (unit + '.json')
            trusted(entry)
            actor = json.loads(entry.read_text())
            if actor.get('protocol') != 1:
                raise Deferred('BOOTSTRAP_REQUIRED inactive legacy')
            for field in ('source', 'library'):
                trusted(Path(actor[field]))
                if digest(actor[field]) != actor[field + '_sha256']:
                    raise Deferred('BOOTSTRAP_REQUIRED enrollment drift')
        executable = re.search(r'(?:^|[ {;])path=([^ ;}]+)', props.get('ExecStart', ''))
        if not executable or Path(executable.group(1)).resolve(strict=True) != Path(actor['source']).resolve(strict=True):
            raise Deferred('BOOTSTRAP_REQUIRED service startup drift')
        group = props['ControlGroup']
        if group:
            relative = Path(group.lstrip('/'))
            if not group.startswith('/') or '..' in relative.parts:
                raise Deferred('invalid cgroup')
            def collect(path):
                members.update(int(item) for item in (path / 'cgroup.procs').read_text().split())
                for child in path.iterdir():
                    if child.is_dir():
                        collect(child)
            collect(cgroups / relative)
        elif pid:
            raise Deferred('missing cgroup')
        if pid and pid not in members:
            raise Deferred('MainPID missing from cgroup')
    processes = {}
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        pid = int(item.name)
        try:
            parent, _ = identity(pid, proc)
            command = [arg for arg in (item / 'cmdline').read_bytes().split(b'\0') if arg]
            if not command or command == [b'']:
                continue  # Kernel thread (no userspace command/cwd).
            cwd = os.readlink(item / 'cwd')
        except FileNotFoundError:
            if not (item / 'stat').exists():
                continue
            raise
        except Deferred:  # verified zombie/exited process
            continue
        # Kernel threads have no user cmdline/cwd.
        if not command or command == [b'']:
            continue
        processes[pid] = (parent, command, cwd.removesuffix(' (deleted)'))
    descendants = set(mains)
    while True:
        next_set = descendants | {p for p, row in processes.items() if row[0] in descendants}
        if next_set == descendants:
            break
        descendants = next_set
    for pid, (parent, command, cwd) in processes.items():
        runner_command = any(Path(os.fsdecode(arg)).name in ('pipeline-runner.sh', 'pipeline-runner.sh.local') for arg in command if arg)
        if runner_command and pid not in descendants:
            raise Deferred('BOOTSTRAP_REQUIRED unmanaged runner')
        if not quiescent:
            continue
        if cwd.startswith('/tmp/aads-wt-'):
            raise Deferred('live worktree worker')
    if quiescent:
        allowed = set(mains)
        for pid, (parent, command, _) in processes.items():
            # Only the exact control waiter is exempt. Names such as sleep or
            # flock alone cannot conceal a worker or an unrecognized child.
            if parent in mains and command in ([b'flock', b'-s', b'201'], [b'/usr/bin/flock', b'-s', b'201']):
                if (Path(os.readlink(proc / str(pid) / 'exe')).name == 'flock'
                        and same_fd(pid, 201, root / 'admission.lock', proc)
                        and not same_fd(pid, 200, root / 'lifetime.lock', proc)):
                    allowed.add(pid)
        if (members | descendants) - allowed:
            raise Deferred('live or unknown descendant')
    return 'AWARE'


def restart_plan(service):
    if not re.fullmatch(r'[A-Za-z0-9_.@:-]+', service):
        raise Deferred('invalid service')
    query = subprocess.run(['systemctl', 'show', service, '-p', 'LoadState', '-p', 'MainPID', '-p', 'ExecStart'],
                           capture_output=True, text=True, timeout=3)
    if query.returncode:
        raise Deferred('restart plan service query failed')
    props = dict(line.split('=', 1) for line in query.stdout.splitlines() if '=' in line)
    executable = re.search(r'(?:^|[ {;])path=([^ ;}]+)', props.get('ExecStart', ''))
    if props.get('LoadState') != 'loaded' or not props.get('MainPID', '').isdigit() or not executable:
        raise Deferred('restart plan incomplete')
    source = Path(executable.group(1)).resolve(strict=True)
    library = source.parent / 'runner_busy_lib.sh'
    trusted(source); trusted(library)
    return dict(service=service, previous_pid=int(props['MainPID']), source=str(source),
                source_sha256=digest(source), library_sha256=digest(library))


def main():
    mode, root = sys.argv[1], Path(sys.argv[2])
    if mode == 'init':
        initialize(root)
    elif mode == 'record':
        record(root, int(sys.argv[3]), *sys.argv[4:7])
    elif mode in ('preflight', 'quiescent'):
        extra = [Path(value) for value in sys.argv[4:6]]
        print(inventory(root, sys.argv[3], mode == 'quiescent', *extra))
    elif mode == 'restart-plan':
        print(json.dumps(restart_plan(sys.argv[3]), sort_keys=True))
    elif mode == 'restart-verify':
        plan = json.loads(sys.argv[4])
        if set(plan) != {'service', 'previous_pid', 'source', 'source_sha256', 'library_sha256'} or plan['service'] != sys.argv[3]:
            raise Deferred('invalid restart plan')
        extra = [Path(value) for value in sys.argv[5:7]]
        deadline = time.monotonic() + 10
        while True:
            try:
                result = inventory(root, sys.argv[3], True, *extra, restart=plan)
                print(result)
                break
            except (Deferred, OSError):
                if time.monotonic() >= deadline:
                    raise Deferred('restart capability not certified')
                time.sleep(.1)
    else:
        raise Deferred('unsupported protocol operation')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('maintenance defer: ' + (str(exc) if isinstance(exc, Deferred) else type(exc).__name__), file=sys.stderr)
        print('BOOTSTRAP_REQUIRED' if isinstance(exc, (FileNotFoundError, Deferred)) else 'UNKNOWN')
        sys.exit(3)
PY_RUNNER_MAINTENANCE
}

runner_maintenance_record() {
    runner_maintenance_metadata record "$RUNNER_MAINTENANCE_ROOT" "$$" "$1" \
        "$RUNNER_MAINTENANCE_SOURCE" "${BASH_SOURCE[0]}" >/dev/null
}

runner_maintenance_enter() {
    # The optional argument is used only by local fixtures; production startup
    # always calls with the fixed host-shared path, independent of engine/env.
    RUNNER_MAINTENANCE_ROOT="${1:-/run/aads-runner-maintenance}"
    RUNNER_MAINTENANCE_SOURCE="$2"
    runner_maintenance_metadata init "$RUNNER_MAINTENANCE_ROOT" || return 3
    exec 201>"$RUNNER_MAINTENANCE_ROOT/admission.lock"
    runner_maintenance_record WAIT_ADMISSION || return 3
    flock -s 201 || return 3
    exec 200>"$RUNNER_MAINTENANCE_ROOT/lifetime.lock"
    flock -s 200 || return 3
    runner_maintenance_record RUNNING || return 3
    exec 201>&-
    RUNNER_MAINTENANCE_ACTIVE=1
}

runner_maintenance_checkpoint() {
    [[ "${RUNNER_MAINTENANCE_ACTIVE:-0}" == 1 ]] || return 3
    exec 201>"$RUNNER_MAINTENANCE_ROOT/admission.lock"
    if flock -n -s 201; then
        exec 201>&-
        return 0
    fi
    exec 201>&-
    runner_maintenance_record DRAINING || return 3
    # No signals or shutdown finalization. The background shells continue to
    # own the inherited lifetime lease through actual CLI/worker completion.
    while (( ${#_bg_jobs[@]} > 0 )); do
        _reap_bg_jobs
        # A cancelled/timed-out controller must not leave admission paused
        # until a long-running worker finishes. Its disappearance is the lock,
        # not a stale flag or elapsed-time assumption.
        exec 201>"$RUNNER_MAINTENANCE_ROOT/admission.lock"
        if flock -n -s 201; then
            runner_maintenance_record RUNNING || return 3
            exec 201>&-
            return 0
        fi
        exec 201>&-
        (( ${#_bg_jobs[@]} == 0 )) || sleep 0.2
    done
    exec 201>"$RUNNER_MAINTENANCE_ROOT/admission.lock"
    runner_maintenance_record QUIESCENT || return 3
    # Never flock -u: that unlocks the shared open-file description inherited
    # by descendants. Close only this daemon's descriptor and keep MainPID alive.
    exec 200>&-
    flock -s 201 || return 3
    exec 200>"$RUNNER_MAINTENANCE_ROOT/lifetime.lock"
    flock -s 200 || return 3
    runner_maintenance_record RUNNING || return 3
    exec 201>&-
}

runner_maintenance_checkpoint_or_hold() {
    if runner_maintenance_checkpoint; then
        return 0
    fi
    # Exiting the daemon could make systemd kill every remaining worker. Keep
    # MainPID and existing descriptors alive and admit no more business work.
    # Recovery requires investigation; never turn metadata/lock I/O failure into
    # an automatic service restart or an unlocked claim.
    printf '%s\n' 'MAINTENANCE_FAULT_HOLD: preserving MainPID and workers; no claims' >&2
    while true; do
        sleep 1 || :
    done
}

runner_maintenance_transaction() (
    local service="$1" wait_seconds="$2" root="$3"
    shift 3
    [[ "$wait_seconds" =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ ]] || return 3
    [[ "$(runner_maintenance_metadata preflight "$root" "$service")" == AWARE ]] || return 3
    exec 203>"$root/admission.lock"
    flock -w "$wait_seconds" -x 203 || return 3
    [[ "$(runner_maintenance_metadata preflight "$root" "$service")" == AWARE ]] || return 3
    exec 202>"$root/lifetime.lock"
    flock -w "$wait_seconds" -x 202 || return 3
    [[ "$(runner_maintenance_metadata quiescent "$root" "$service")" == AWARE ]] || return 3
    # Callback and all its children inherit BOTH leases. A single remote Bash
    # invocation owns the entire install/restart transaction, not a sidecar SSH.
    "$@"
)
