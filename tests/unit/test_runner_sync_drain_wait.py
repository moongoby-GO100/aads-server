"""sync_pipeline_runner_remote.sh --drain-wait (AADS-RUNNER-SYNC-DRAIN-WAIT-20261020).

실제 스크립트 + 실제 flock 프로토콜을 쓴다. 가짜인 것은 ssh/scp 전송 경계와
systemctl 의 프로세스 소속 정보뿐이다. 원격 호스트에는 아무것도 하지 않는다.
"""
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import pytest

from tests.unit.test_runner_maintenance_protocol import Host, wait_for

ROOT = Path(__file__).resolve().parents[2]
SYNC = ROOT / "scripts/sync_pipeline_runner_remote.sh"
LAUNCHER = ROOT / "scripts/runner_sync_launcher.sh"
TARGET = "fixture|fixture|/root/scripts/pipeline-runner.sh|fixture.service|/nonexistent.service"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("flock") is None or shutil.which("timeout") is None,
    reason="bash/flock/timeout 필요",
)

# 전송 경계. 트랜잭션 페이로드(마지막 줄이 runner_maintenance_transaction 호출)만 로컬에서 실행하되
# 고정 경로·대기 시간·적용 콜백만 픽스처로 바꾼다. 잠금 로직(flock, preflight, quiescent)은 그대로다.
FAKE_SSH = r"""#!/usr/bin/env bash
cmd="${@: -1}"
printf '%s\n' "$*" >> "$FIXTURE/ssh-argv"
[[ "$cmd" == "bash -s" ]] || exit 0
payload=$(cat)
if grep -q '^runner_maintenance_transaction "\$1" ' <<<"$payload"; then
    printf '%s\n' "$payload" > "$FIXTURE/transaction-payload"
    if [[ "${FAKE_TRANSACTION:-real}" == "stub_timeout" ]]; then
        printf 'DRAIN_RESULT rc=3 elapsed=%s\n' "${FAKE_STUB_ELAPSED:-0}"; exit 3
    fi
    if [[ "${FAKE_TRANSACTION:-real}" == "stub_ok" ]]; then
        printf 'DRAIN_ACQUIRED elapsed=41\nDRAIN_APPLIED elapsed=42\nMAINTENANCE_APPLIED changed=1 restart=1\n'; exit 0
    fi
    payload=${payload//\/run\/aads-runner-maintenance/$FIXTURE\/state}
    if [[ -n "${FAKE_WAIT_OVERRIDE:-}" ]]; then
        payload=$(sed "s/^\(runner_maintenance_transaction \"\\\$1\" \)[0-9][0-9]* /\1${FAKE_WAIT_OVERRIDE} /" <<<"$payload")
    fi
    last_two=$(tail -n2 <<<"$payload")
    head_part=$(head -n -2 <<<"$payload")
    inject='eval "$(declare -f runner_maintenance_metadata | sed "1s/runner_maintenance_metadata/actual_metadata/")"
runner_maintenance_metadata() {
  if [[ "$1" == preflight || "$1" == quiescent ]]; then actual_metadata "$@" "$FIXTURE/proc" "$FIXTURE/cgroup"; else actual_metadata "$@"; fi
}
runner_apply_bundle() { printf applied >> "$FIXTURE/applied"; }'
    result=$(bash -s <<<"$head_part"$'\n'"$inject"$'\n'"$last_two"; echo "__rc=$?")
    rc=${result##*__rc=}
    result=${result%__rc=*}
    if [[ -n "${FAKE_REPORT_ELAPSED:-}" ]]; then
        result=$(sed "s/^\(DRAIN_RESULT rc=[0-9]* elapsed=\)[0-9]*/\1${FAKE_REPORT_ELAPSED}/" <<<"$result")
    fi
    printf '%s\n' "$result"
    exit "$rc"
fi
if grep -q 'runner_live_process_probe "\$1"' <<<"$payload"; then echo "${FAKE_PROCESS_STATE:-BUSY}"; exit 0; fi
if grep -q 'runner_maintenance_metadata preflight' <<<"$payload"; then echo AWARE; exit 0; fi
exit 0
"""

FAKE_SSH_PLAIN = FAKE_SSH.replace(
    '[[ "$cmd" == "bash -s" ]] || exit 0',
    'case "$cmd" in *"hostname -s"*) echo fixture; exit 0;; *"systemctl is-active"*) echo active; exit 0;; sha256sum*) echo stale; exit 0;; esac\n[[ "$cmd" == "bash -s" ]] || exit 0',
)

FAKE_DOCKER = "#!/usr/bin/env bash\necho \"${FAKE_BUSY_COUNT:-3}\"\n"
FAKE_SCP = "#!/usr/bin/env bash\nexit 0\n"
FAKE_TIMEOUT = '#!/usr/bin/env bash\nprintf "%s\\n" "$3" >> "$FIXTURE/timeout-values"\nexec /usr/bin/timeout "$@"\n'


@pytest.fixture
def host(tmp_path, monkeypatch):
    # quiescent probe treats any process whose cwd is under /tmp/aads-wt-* as a live worker;
    # runner worktrees (and this task's own checkout) live there, so fixture actors must not inherit it.
    monkeypatch.chdir(tmp_path)
    value = Host(tmp_path)
    for name, body in (("ssh", FAKE_SSH_PLAIN), ("scp", FAKE_SCP), ("docker", FAKE_DOCKER), ("timeout", FAKE_TIMEOUT)):
        (value.bin / name).write_text(body)
        (value.bin / name).chmod(0o755)
    try:
        yield value
    finally:
        value.finish()


def sync_env(host, **extra):
    return {
        **host.env,
        "AADS_RUNNER_SYNC_EXPORTED": "1",
        "CANONICAL_RUNNER": str(ROOT / "scripts/pipeline-runner.sh"),
        "AADS_RUNNER_SYNC_LOCK": str(host.path / "sync.lock"),
        "AADS_RUNNER_SYNC_TARGETS": TARGET,
        **extra,
    }


def start_sync(host, *args, **extra):
    return subprocess.Popen(
        ["bash", str(SYNC), *args], env=sync_env(host, **extra),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        cwd=host.path,
    )


def kill_tree(pid):
    """ssh 연결 단절의 모의: ssh(+timeout) 와 원격 bash, flock 대기자까지 후손 전체를 죽인다."""
    rows = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                rows[int(entry.name)] = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (FileNotFoundError, ProcessLookupError, IndexError):
                pass
    victims, frontier = set(), {pid}
    while frontier:
        victims |= frontier
        frontier = {p for p, parent in rows.items() if parent in frontier} - victims
    for victim in victims:
        try:
            os.kill(victim, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_sync(host, *args, **extra):
    proc = start_sync(host, *args, **extra)
    out, err = proc.communicate(timeout=60)
    return proc.returncode, out, err


def summary(out):
    lines = [line for line in out.splitlines() if "sync summary:" in line]
    assert lines, out
    return lines[-1]


def admission_is_held_exclusively(host):
    with open(host.state / "admission.lock", "a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(stream, fcntl.LOCK_UN)
        return False


def claims(host):
    path = host.path / "claims"
    return len(path.read_text().splitlines()) if path.exists() else 0


# ── 인자 검증 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    ["--drain-wait", "59", "--target", "x"],
    ["--drain-wait", "3601", "--target", "x"],
    ["--drain-wait", "0", "--target", "x"],
    ["--drain-wait", "-60", "--target", "x"],
    ["--drain-wait", "abc", "--target", "x"],
    ["--drain-wait", "60.5", "--target", "x"],
    ["--drain-wait", "", "--target", "x"],
    ["--drain-wait"],
    ["--target", "x", "--drain-wait"],
])
def test_drain_wait_rejects_out_of_range_or_malformed_value(host, args):
    rc, out, err = run_sync(host, *args)
    assert rc == 2, out + err
    assert "--drain-wait requires an integer 60..3600" in err
    assert not (host.path / "ssh-argv").exists()


@pytest.mark.parametrize("seconds", ["60", "3600", "0090"])
def test_drain_wait_boundaries_accepted_with_target(host, seconds):
    rc, out, err = run_sync(host, "--drain-wait", seconds, "--target", "fixture", "--dry-run")
    assert rc == 0, out + err
    assert "fixture=synced" in summary(out)


def test_drain_wait_without_target_is_refused_before_any_host_is_touched(host):
    rc, out, err = run_sync(host, "--drain-wait", "120")
    assert rc == 2, out + err
    assert "--drain-wait requires --target" in err
    assert not (host.path / "ssh-argv").exists()
    rc, out, err = run_sync(host, "--drain-wait", "120", "--dry-run")
    assert rc == 2 and not (host.path / "ssh-argv").exists()


def test_drain_wait_env_var_does_not_enable_drain(host):
    rc, out, err = run_sync(host, "--target", "fixture", AADS_RUNNER_SYNC_DRAIN_WAIT="120", DRAIN_WAIT="120")
    assert rc == 0, out + err
    assert "fixture=deferred" in summary(out)
    assert "DRAIN" not in out


# ── 기본 경로(플래그 없음)는 기존과 동일 ────────────────────────────────

def test_default_path_still_defers_when_live_process_is_busy(host):
    rc, out, err = run_sync(host, "--target", "fixture", FAKE_PROCESS_STATE="BUSY")
    assert rc == 0, out + err
    assert "sync deferred — live process state=BUSY; DB/ignore-busy cannot override" in out
    assert "fixture=deferred" in summary(out)
    assert not (host.path / "transaction-payload").exists()


def test_default_path_ignore_busy_does_not_override_live_process_busy(host):
    rc, out, err = run_sync(host, "--target", "fixture", "--ignore-busy", FAKE_PROCESS_STATE="BUSY")
    assert rc == 0, out + err
    assert "fixture=deferred" in summary(out)
    assert not (host.path / "transaction-payload").exists()


def test_default_transaction_payload_is_unchanged_30s_apply_bundle(host):
    rc, out, err = run_sync(host, "--target", "fixture", FAKE_PROCESS_STATE="IDLE", FAKE_BUSY_COUNT="0", FAKE_TRANSACTION="stub_ok")
    assert rc == 0, out + err
    payload = (host.path / "transaction-payload").read_text()
    assert payload.rstrip().endswith('runner_maintenance_transaction "$1" 30 /run/aads-runner-maintenance runner_apply_bundle "$@"')
    assert "runner_drain_apply" not in payload.split("\n")[-1]
    assert not (host.path / "timeout-values").exists()
    assert "DRAIN start" not in out and "DRAIN mode" not in out


# ── 드레인 경로 ──────────────────────────────────────────────────────────

def test_drain_enters_transaction_even_when_live_process_and_db_report_busy(host):
    rc, out, err = run_sync(host, "--target", "fixture", "--drain-wait", "120",
                            FAKE_PROCESS_STATE="BUSY", FAKE_BUSY_COUNT="5", FAKE_TRANSACTION="stub_ok")
    assert rc == 0, out + err
    assert "sync deferred" not in out
    assert "fixture=synced" in summary(out)
    payload = (host.path / "transaction-payload").read_text()
    last = [line for line in payload.splitlines() if line.startswith("runner_maintenance_transaction \"$1\"")][-1]
    assert last == 'runner_maintenance_transaction "$1" 120 /run/aads-runner-maintenance runner_drain_apply "$@"; rc=$?'
    for marker in ("DRAIN start wait=120s", "DRAIN acquired elapsed=41s", "DRAIN applied elapsed=42s"):
        assert marker in out, out


def test_drain_skips_busy_prefilter_but_keeps_aware_preflight(host):
    # 레거시 액터(AWARE 아님)는 드레인이어도 트랜잭션에 들어가지 않는다 — 우회 금지.
    legacy = FAKE_SSH_PLAIN.replace("then echo AWARE; exit 0; fi", "then echo LEGACY; exit 0; fi")
    (host.bin / "ssh").write_text(legacy)
    rc, out, err = run_sync(host, "--target", "fixture", "--drain-wait", "120", FAKE_PROCESS_STATE="BUSY")
    assert rc == 0, out + err
    assert "BOOTSTRAP_REQUIRED" in out and "fixture=deferred" in summary(out)
    assert not (host.path / "transaction-payload").exists()


def test_drain_ssh_hard_timeout_exceeds_wait_plus_120(host):
    rc, out, err = run_sync(host, "--target", "fixture", "--drain-wait", "600", FAKE_TRANSACTION="stub_ok")
    assert rc == 0, out + err
    values = [int(v) for v in (host.path / "timeout-values").read_text().split()]
    assert values and all(v >= 600 + 120 for v in values), values
    argv = (host.path / "ssh-argv").read_text()
    assert "ServerAliveInterval=30" in argv


def test_drain_dry_run_prints_plan_only(host):
    rc, out, err = run_sync(host, "--target", "fixture", "--drain-wait", "300", "--dry-run", FAKE_PROCESS_STATE="BUSY")
    assert rc == 0, out + err
    assert "DRY_RUN fixture: would DRAIN" in out and "300s" in out
    assert not (host.path / "transaction-payload").exists()
    assert not (host.path / "timeout-values").exists()


def test_drain_timeout_is_reported_in_summary_and_does_not_fail(host):
    rc, out, err = run_sync(host, "--target", "fixture", "--drain-wait", "60",
                            FAKE_TRANSACTION="stub_timeout", FAKE_STUB_ELAPSED="61")
    assert rc == 0, out + err
    assert "fixture=drain_timeout" in summary(out)
    assert "DRAIN timeout elapsed=61s" in out
    assert "drain_timeout=1" in out and "synced=0" in out


def test_instant_lease_failure_is_deferred_not_drain_timeout(host):
    rc, out, err = run_sync(host, "--target", "fixture", "--drain-wait", "60",
                            FAKE_TRANSACTION="stub_timeout", FAKE_STUB_ELAPSED="0")
    assert rc == 0, out + err
    assert "fixture=deferred" in summary(out)
    assert "drain_timeout" not in summary(out)


# ── 실제 flock: 타임아웃·연결 단절 시 claim 재개 ─────────────────────────

def phase(host, actor):
    import json
    return json.loads((host.state / "actors" / f"{actor.pid}.json").read_text())["phase"]


def test_real_drain_stops_claims_then_applies_after_worker_finishes(host):
    actor = host.actor(worker=True)
    proc = start_sync(host, "--target", "fixture", "--drain-wait", "60",
                      FAKE_WAIT_OVERRIDE="20", FAKE_PROCESS_STATE="BUSY")
    try:
        wait_for(lambda: admission_is_held_exclusively(host), timeout=20)
        time.sleep(.3)
        before = claims(host)
        time.sleep(.5)
        assert claims(host) == before  # 신규 claim 중단
        assert proc.poll() is None and not (host.path / "applied").exists()
        (host.path / "worker-done").touch()
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
            proc.wait()
    assert proc.returncode == 0, out + err
    assert (host.path / "applied").read_text() == "applied"
    assert "DRAIN acquired elapsed=" in out and "DRAIN applied elapsed=" in out
    assert "fixture=synced" in summary(out)
    wait_for(lambda: phase(host, actor) == "RUNNING")
    base = claims(host)
    wait_for(lambda: claims(host) > base)
    assert actor.poll() is None


def test_real_drain_timeout_releases_locks_and_claims_resume(host):
    actor = host.actor(worker=True)  # 워커가 끝나지 않는다
    proc = start_sync(host, "--target", "fixture", "--drain-wait", "60",
                      FAKE_WAIT_OVERRIDE="2", FAKE_REPORT_ELAPSED="61", FAKE_PROCESS_STATE="BUSY")
    try:
        wait_for(lambda: admission_is_held_exclusively(host), timeout=20)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
            proc.wait()
    assert proc.returncode == 0, out + err
    assert "fixture=drain_timeout" in summary(out), out
    assert not (host.path / "applied").exists()
    assert not admission_is_held_exclusively(host)
    wait_for(lambda: phase(host, actor) == "RUNNING")
    base = claims(host)
    wait_for(lambda: claims(host) > base)
    worker = int((host.path / "worker.pid").read_text())
    assert actor.poll() is None and Path(f"/proc/{worker}").exists()


def test_remote_shell_death_closes_lock_fds_and_claims_resume(host):
    actor = host.actor(worker=True)
    proc = start_sync(host, "--target", "fixture", "--drain-wait", "600",
                      FAKE_WAIT_OVERRIDE="600", FAKE_PROCESS_STATE="BUSY")
    try:
        wait_for(lambda: admission_is_held_exclusively(host), timeout=20)
        time.sleep(.3)
        stalled = claims(host)
        time.sleep(.3)
        assert claims(host) == stalled
        # ssh 연결 단절 = 원격 bash 와 그 자식(flock 대기자)의 소멸. 같은 프로세스 그룹을 모두 죽인다.
        kill_tree(proc.pid)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
            proc.wait()
    wait_for(lambda: not admission_is_held_exclusively(host))
    wait_for(lambda: phase(host, actor) == "RUNNING")
    wait_for(lambda: claims(host) > stalled)
    assert not (host.path / "applied").exists()
    assert actor.poll() is None


# ── 타이머 경로 불변 ─────────────────────────────────────────────────────

def test_timer_launcher_path_never_passes_drain_wait():
    for path in (LAUNCHER, ROOT / "scripts/systemd/aads-pipeline-runner-sync.service",
                 ROOT / "scripts/aads-pipeline-runner-sync.timer"):
        assert "drain" not in path.read_text(encoding="utf-8").lower(), path
    assert "ExecStart=/usr/local/sbin/aads-runner-sync-launcher\n" in (
        ROOT / "scripts/systemd/aads-pipeline-runner-sync.service").read_text(encoding="utf-8")
    assert "DRAIN_WAIT=0" in SYNC.read_text(encoding="utf-8")


def test_usage_and_agents_document_drain_wait():
    usage = subprocess.run(["bash", str(SYNC), "--help"], capture_output=True, text=True).stdout
    assert "--drain-wait SECONDS" in usage and "--target X --drain-wait N" in usage
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.count("--target X --drain-wait N") == 1
