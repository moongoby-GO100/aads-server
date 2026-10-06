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
