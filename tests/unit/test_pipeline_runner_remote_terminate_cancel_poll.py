"""AADS-RUNNER-REMOTE-TERMINATE-CANCEL-POLL-20261013.

원격 러너(contabo14)는 terminate_task 가 PID 를 죽일 수 없다. DB 가 error 인데도 래퍼가 살아남아
claude 가 죽자 codex 로 폴백해 작업을 다시 시작했다(runner-9b415884). 러너가 스스로 상태를 보고 끝나야 한다.

실제 러너 함수 본문을 스크립트에서 잘라내 bash 하니스에서 돌리고, DB 는 sqlite, 모델은 sleep 으로 대신한다.
"""

import asyncio
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")

WAIT_START = "wait_runner_cli_process() {"
WATCH_END = "# ── 원격 종료 자기 감시 END ──"
ABORT_START = "_abort_job_terminal_before_work() {"
ABORT_END = "# 워크트리 준비 직전 확인"


def _read(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _blocks(name: str) -> str:
    s = _read(name)
    w = s[s.index(WAIT_START) : s.index(WATCH_END) + len(WATCH_END)]
    a = s[s.index(ABORT_START) : s.index(ABORT_END, s.index(ABORT_START))]
    return w + "\n" + a


def _run_job_body(script: str) -> str:
    return script[script.index("\nrun_job() {") :]


def test_scripts_are_byte_identical():
    assert (ROOT / "scripts/pipeline-runner.sh").read_bytes() == (ROOT / "scripts/pipeline-runner.sh.local").read_bytes()


# ── 배선(정적): 시도 직전 / 모델 종료 직후 / codex 재시도 직전 ───────────────────────────

@pytest.mark.parametrize("name", SCRIPTS)
def test_run_job_checks_cancel_before_attempt_after_exit_and_before_codex_retry(name):
    body = _run_job_body(_read(name))
    check = '_cancel_abort_if_terminal "$job_id" "$project" "$parallel_group"'
    assert body.count(check) == 4
    loop = body.index("while [[ $attempt -lt $total_attempts ]]; do")
    first = body.index(check, loop)
    assert first < body.index('local current_model="${MODEL_CYCLE[$attempt]}"', loop)
    assert first < body.index("MODEL_ATTEMPT job=", loop)

    wait_call = body.index("wait_runner_cli_process", loop)
    second = body.index(check, wait_call)
    assert wait_call < second < body.index("restore_runner_claude_output", wait_call)

    retry_loop = body.index("while [[ $exit_code -ne 0 && $_codex_retry -lt 12 ]]; do")
    third = body.index(check, retry_loop)
    sleep5 = body.index("sleep 5", retry_loop)
    assert third < sleep5
    # 대기(sleep 5) 중에 바뀐 status 도 codex 를 띄우기 전에 본다.
    fourth = body.index(check, sleep5)
    assert sleep5 < fourth < body.index('timeout "$MAX_RUNTIME" codex', sleep5)
    # 취소는 후처리 없이 return 한다.
    for m in range(4):
        pos = [i for i in range(len(body)) if body.startswith(check, i)][m]
        assert "return 1" in body[pos : pos + len(check) + 120]


@pytest.mark.parametrize("name", SCRIPTS)
def test_watcher_is_started_and_reaped_by_wait_function(name):
    s = _read(name)
    fn = s[s.index(WAIT_START) : s.index("\nget_job_status() {")]
    assert '_cli_cancel_watch_loop "$job_id" "$pid" "$owner_pid" "$cw_marker" &' in fn
    assert 'kill "$cw_pid"' in fn and 'wait "$cw_pid"' in fn
    # 감시 루프는 모델/소유 셸 종료와 시간 상한으로 스스로 끝난다(R-BG).
    loop = s[s.index("_cli_cancel_watch_loop() {") : s.index("_cancel_abort_if_terminal() {")]
    assert 'kill -0 "$_cli_pid"' in loop and 'kill -0 "$_owner_pid"' in loop
    assert "MAX_RUNTIME" in loop


# ── 하니스 ───────────────────────────────────────────────────────────────────

HARNESS = r"""
set -u
DB="$1"; JOB="$2"; OUT="$3"; MODEL="$4"
export RUNNER_CLI_HEARTBEAT_SEC=0
log() { echo "LOG: $*" >> "$OUT/log"; [[ -e "$OUT/db_down_on_fire" && "$*" == *CANCEL_WATCH_FIRED* ]] && touch "$OUT/db_down"; return 0; }
record_runner_event() { echo "EVENT: $2 status=$3" >> "$OUT/events"; }
_release_work_lock() { echo "RELEASE: $1 $2 $3" >> "$OUT/events"; }
_reclaim_finished_worktree() { echo "RECLAIM: $1" >> "$OUT/events"; }
_cli_heartbeat_loop() { :; }
if ! command -v ps >/dev/null 2>&1; then
ps() {
    python3 - "$@" <<'PY'
import os, sys
a = sys.argv[1:]
def info(pid):
    try:
        raw = open("/proc/%s/stat" % pid).read()
        rest = raw[raw.rindex(")") + 2:].split()
        return rest[0], int(rest[1])
    except Exception:
        return None
if a[:1] == ["-p"]:
    i = info(a[1])
    if i:
        print(i[0])
    sys.exit(0 if i else 1)
for d in sorted(os.listdir("/proc")):
    if d.isdigit() and info(d):
        print(d, info(d)[1])
PY
}
fi
db_exec() {
    [[ -e "$OUT/db_down" ]] && return 1
    [[ -e "$OUT/db_hang" ]] && { sleep 7010; return 1; }
    python3 - "$DB" "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=10)
for r in c.execute(sys.argv[2]).fetchall():
    print(r[0])
PY
}
BLOCKS
_current_job_id="$JOB"; _current_session_id="s"
for attempt in 0 1; do
    if _cancel_abort_if_terminal "$JOB" AADS grp; then echo ABORT_TOP >> "$OUT/result"; break; fi
    echo "attempt$attempt" >> "$OUT/attempts"
    bash -c "$MODEL" > "$OUT/o" 2> "$OUT/e" &
    pid=$!
    rc=0
    wait_runner_cli_process "$JOB" "$pid" "$OUT/o" "$OUT/e" m m M 1 2 1 fake 0 0 || rc=$?
    echo "rc=$rc" >> "$OUT/result"
    if _cancel_abort_if_terminal "$JOB" AADS grp; then echo ABORT_POST >> "$OUT/result"; break; fi
    [[ $rc -eq 0 ]] && break
done
"""


def _setup(tmp_path: Path, status: str = "running"):
    db = tmp_path / "db.sqlite"
    out = tmp_path / "out"
    out.mkdir()
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE pipeline_jobs (job_id TEXT PRIMARY KEY, status TEXT)")
    c.execute("INSERT INTO pipeline_jobs VALUES ('runner-t1', ?)", (status,))
    c.commit()
    c.close()
    return db, out


def _set_status(db: Path, status: str):
    c = sqlite3.connect(db, timeout=10)
    c.execute("UPDATE pipeline_jobs SET status=? WHERE job_id='runner-t1'", (status,))
    c.commit()
    c.close()


def _spawn(db: Path, out: Path, name: str, model: str, env_extra: dict):
    script = HARNESS.replace("BLOCKS", _blocks(name))
    env = {**os.environ, **env_extra, "RUNNER_TEST_ID": _test_id(out)}
    return subprocess.Popen(
        ["bash", "-c", script, "bash", str(db), "runner-t1", str(out), model],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _test_id(out: Path) -> str:
    return "rt-%s" % out.parent.name


def _leaked(test_id: str) -> list[str]:
    """하니스가 끝난 뒤에도 살아 있는 후손(감시 루프·모델·하위 프로세스). ps 가 없는 이미지에서도 동작하도록 /proc 을 본다."""
    needle = ("RUNNER_TEST_ID=%s" % test_id).encode()
    found = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            env = Path("/proc/%s/environ" % d).read_bytes().split(b"\0")
            raw = Path("/proc/%s/stat" % d).read_text()
            state = raw[raw.rindex(")") + 2 :].split()[0]
            cmd = Path("/proc/%s/cmdline" % d).read_bytes().replace(b"\0", b" ").decode(errors="replace")[:80]
        except OSError:
            continue
        if needle in env and state != "Z":
            found.append("%s %s" % (d, cmd))
    return found


def _assert_no_leak(test_id: str):
    deadline = time.time() + 3
    while _leaked(test_id) and time.time() < deadline:
        time.sleep(0.2)
    leaked = _leaked(test_id)
    for ln in leaked:
        subprocess.run(["kill", "-9", ln.split()[0]])
    assert leaked == []


def _rd(path: Path) -> str:
    return path.read_text() if path.exists() else ""


# ── (a) 시도 사이 확인 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", SCRIPTS)
@pytest.mark.parametrize("status", ["error", "cancelled", "queued", "rejected"])
def test_non_running_status_aborts_before_any_attempt(tmp_path, name, status):
    db, out = _setup(tmp_path, status)
    p = _spawn(db, out, name, "sleep 1", {})
    p.communicate(timeout=60)
    assert "ABORT_TOP" in _rd(out / "result")
    assert not (out / "attempts").exists()  # 폴백 시도 시작 안 함
    log = _rd(out / "log")
    assert f"CANCELLED_BY_STATUS job=runner-t1 status={status}" in log
    events = _rd(out / "events")
    assert "RELEASE: AADS runner-t1 grp" in events and "RECLAIM: runner-t1" in events
    assert "EVENT: cancelled_by_status" in events


@pytest.mark.parametrize("name", SCRIPTS)
def test_running_job_is_not_aborted(tmp_path, name):
    db, out = _setup(tmp_path, "running")
    p = _spawn(db, out, name, "sleep 1", {"RUNNER_CANCEL_POLL_SEC": "1"})
    p.communicate(timeout=60)
    assert "ABORT" not in _rd(out / "result")
    assert "rc=0" in _rd(out / "result")
    assert "CANCELLED_BY_STATUS" not in _rd(out / "log")


# ── (b) 감시 루프: 실행 중 status 변경 → 모델/하위/감시 루프 모두 소멸, 폴백 없음 ───────────

def _flip_and_wait(db, out, p, delay=1.0, timeout=60):
    time.sleep(delay)
    _set_status(db, "error")
    t0 = time.time()
    p.communicate(timeout=timeout)
    return time.time() - t0


@pytest.mark.parametrize("name", SCRIPTS)
def test_cancel_kills_model_tree_and_watcher_without_fallback(tmp_path, name):
    db, out = _setup(tmp_path)
    # 하위 프로세스(pytest 역할)를 가진 모델
    p = _spawn(db, out, name, "sleep 7001.1 & wait", {"RUNNER_CANCEL_POLL_SEC": "2"})
    tid = _test_id(out)
    elapsed = _flip_and_wait(db, out, p)
    assert elapsed < 20, elapsed
    assert _rd(out / "result").splitlines()[-1] == "ABORT_POST"
    assert _rd(out / "attempts").splitlines() == ["attempt0"]  # attempt1(폴백) 미시작
    assert "CANCEL_WATCH_FIRED job=runner-t1 status=error" in _rd(out / "log")
    _assert_no_leak(tid)  # 모델·하위·감시 루프 모두 소멸


@pytest.mark.parametrize("name", SCRIPTS)
def test_cancel_escalates_to_sigkill_when_sigterm_ignored(tmp_path, name):
    db, out = _setup(tmp_path)
    # SIGTERM 무시는 exec 한 sleep 에도 상속된다 — SIGKILL 단계가 있어야 끝난다.
    model = "trap '' TERM; sleep 7002.2 & wait; sleep 7002.3"
    p = _spawn(db, out, name, model, {"RUNNER_CANCEL_POLL_SEC": "1", "RUNNER_CANCEL_KILL_GRACE_SEC": "2"})
    tid = _test_id(out)
    elapsed = _flip_and_wait(db, out, p)
    assert elapsed < 20, elapsed
    assert _rd(out / "attempts").splitlines() == ["attempt0"]
    _assert_no_leak(tid)


def test_default_poll_interval_ends_within_15s_plus_margin(tmp_path):
    db, out = _setup(tmp_path)
    p = _spawn(db, out, "pipeline-runner.sh", "sleep 7003.4", {})
    tid = _test_id(out)
    elapsed = _flip_and_wait(db, out, p, delay=0.5, timeout=90)
    assert elapsed <= 15 + 6, elapsed
    assert _rd(out / "attempts").splitlines() == ["attempt0"]
    _assert_no_leak(tid)


# ── (c) DB 조회 실패는 취소가 아니다 ───────────────────────────────────────────

@pytest.mark.parametrize("name", SCRIPTS)
def test_db_outage_does_not_kill_healthy_job(tmp_path, name):
    db, out = _setup(tmp_path, "error")  # DB 값은 error 지만 읽을 수 없다
    (out / "db_down").touch()
    p = _spawn(db, out, name, "sleep 4", {"RUNNER_CANCEL_POLL_SEC": "1"})
    tid = _test_id(out)
    p.communicate(timeout=60)
    assert _rd(out / "result").splitlines() == ["rc=0"]  # 정상 종료, 취소·재시도 없음
    log = _rd(out / "log")
    assert "CANCEL_WATCH_FIRED" not in log and "CANCELLED_BY_STATUS" not in log
    assert _rd(out / "attempts").splitlines() == ["attempt0"]
    _assert_no_leak(tid)


@pytest.mark.parametrize("name", SCRIPTS)
def test_model_finishing_normally_leaves_no_watcher(tmp_path, name):
    db, out = _setup(tmp_path)
    p = _spawn(db, out, name, "sleep 1", {"RUNNER_CANCEL_POLL_SEC": "1"})
    tid = _test_id(out)
    p.communicate(timeout=60)
    _assert_no_leak(tid)
    assert not list(out.glob("*.cancel_fired"))


# ── 2. terminate_task ────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, row):
        self.row = row
        self.executed = []

    async def fetchrow(self, sql, *args):
        return self.row if "pipeline_jobs" in sql else None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _run_terminate(monkeypatch, row):
    import app.core.db_pool as db_pool
    import app.services.pipeline_runner_service as prs
    import app.services.task_logger as tl
    from app.services import tool_executor as te

    conn = _Conn(row)
    monkeypatch.setattr(db_pool, "get_pool", lambda: _Pool(conn))

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(prs, "_reconcile_job_goal_links", _noop)
    monkeypatch.setattr(tl, "emit_task_log", _noop)
    monkeypatch.setattr(tl, "emit_task_completed", _noop)
    monkeypatch.setattr(te, "_local_pid_alive", lambda pid: False)

    async def _go():
        return await te.ToolExecutor()._terminate_task({"task_id": row["job_id"], "reason": "stall"})

    return asyncio.run(_go()), conn


def _status_update(conn):
    return [e for e in conn.executed if "SET status='error'" in e[0]][0]


def test_terminate_remote_keeps_runner_pid_and_reports_honestly(monkeypatch):
    row = {"job_id": "runner-9b415884", "status": "running", "phase": "claude_code_work", "project": "GO100", "runner_pid": 1211801}
    res, conn = _run_terminate(monkeypatch, row)
    sql, _ = _status_update(conn)
    assert "runner_pid" not in sql  # 감사·추적용으로 보존
    assert not any("runner_pid=NULL" in e[0] for e in conn.executed)
    assert res["result"] != "terminated"
    assert res["remote_process_killed"] is False
    assert res["runner_pid"] == 1211801
    assert "원격 러너는 다음 상태 확인(≤15초)에서 자기 종료" in res["message"]


def test_terminate_aads_local_still_clears_runner_pid(monkeypatch):
    row = {"job_id": "runner-aaaa0001", "status": "running", "phase": "claude_code_work", "project": "AADS", "runner_pid": 4242}
    res, conn = _run_terminate(monkeypatch, row)
    sql, _ = _status_update(conn)
    assert "runner_pid=NULL" in sql
    assert res["result"] == "terminated"


def test_terminate_already_finished_remote_is_untouched(monkeypatch):
    row = {"job_id": "runner-bbbb0002", "status": "error", "phase": "error", "project": "GO100", "runner_pid": 77}
    res, conn = _run_terminate(monkeypatch, row)
    assert res["result"] == "already_finished"
    assert conn.executed == []


# ── 리뷰 지적 1: 감시자가 취소해 모델을 죽였으면, 이후 DB 조회가 실패해도 다음 모델을 띄우지 않는다 ─────────

@pytest.mark.parametrize("name", SCRIPTS)
def test_cancel_latch_survives_db_outage_after_fire(tmp_path, name):
    db, out = _setup(tmp_path)
    (out / "db_down_on_fire").touch()  # 발화 직후 DB 가 끊긴다 → 사후 확인 조회는 빈 값
    p = _spawn(db, out, name, "sleep 7004.1", {"RUNNER_CANCEL_POLL_SEC": "1"})
    tid = _test_id(out)
    _flip_and_wait(db, out, p)
    assert (out / "db_down").exists()
    assert _rd(out / "result").splitlines()[-1] == "ABORT_POST"
    assert _rd(out / "attempts").splitlines() == ["attempt0"]  # 폴백(attempt1) 미시작
    assert "CANCELLED_BY_STATUS job=runner-t1 status=error" in _rd(out / "log")
    _assert_no_leak(tid)


@pytest.mark.parametrize("name", SCRIPTS)
def test_latch_not_set_when_watcher_did_not_fire(tmp_path, name):
    db, out = _setup(tmp_path)
    p = _spawn(db, out, name, "exit 3", {"RUNNER_CANCEL_POLL_SEC": "1"})
    p.communicate(timeout=60)
    assert "ABORT" not in _rd(out / "result")  # 일반 실패는 취소로 오인하지 않는다
    assert _rd(out / "attempts").splitlines() == ["attempt0", "attempt1"]


# ── 리뷰 지적 3: 멈춘 DB 조회가 감시를 멈추지 않는다 ──────────────────────────────

@pytest.mark.parametrize("name", SCRIPTS)
def test_hung_status_query_is_bounded_and_watch_recovers(tmp_path, name):
    db, out = _setup(tmp_path)
    (out / "db_hang").touch()  # 이후 조회가 영원히 멈춘다
    p = _spawn(db, out, name, "sleep 7005.2", {"RUNNER_CANCEL_POLL_SEC": "1", "RUNNER_CANCEL_QUERY_TIMEOUT_SEC": "2"})
    tid = _test_id(out)
    time.sleep(3.5)  # 감시자의 첫 조회가 멈춰 있는 동안
    assert p.poll() is None
    (out / "db_hang").unlink()
    _set_status(db, "error")
    t0 = time.time()
    p.communicate(timeout=60)
    assert time.time() - t0 < 15
    assert _rd(out / "result").splitlines()[-1] == "ABORT_POST"
    assert _rd(out / "attempts").splitlines() == ["attempt0"]
    _assert_no_leak(tid)  # 멈춘 조회 프로세스(sleep 7010)까지 정리됐다


@pytest.mark.parametrize("name", SCRIPTS)
def test_bounded_status_returns_empty_on_hang_and_value_on_success(tmp_path, name):
    db, out = _setup(tmp_path, "error")
    script = HARNESS.split("_current_job_id=")[0].replace("BLOCKS", _blocks(name)) + r'''
export RUNNER_CANCEL_QUERY_TIMEOUT_SEC=2
echo "ok=[$(_get_job_status_bounded "$JOB")]"
touch "$OUT/db_hang"
t0=$SECONDS
echo "hang=[$(_get_job_status_bounded "$JOB")]"
echo "took=$((SECONDS - t0))"
'''
    env = {**os.environ, "RUNNER_TEST_ID": _test_id(out)}
    r = subprocess.run(["bash", "-c", script, "bash", str(db), "runner-t1", str(out), "x"], env=env, capture_output=True, text=True, timeout=60)
    assert "ok=[error]" in r.stdout and "hang=[]" in r.stdout, r.stdout + r.stderr
    took = int(r.stdout.split("took=")[1].split()[0])
    assert 2 <= took <= 5, took
    _assert_no_leak(_test_id(out))


# ── 리뷰 지적 2: codex 재시도 대기(sleep 5) 중 취소되면 codex 를 새로 띄우지 않는다 ─────────

RETRY_START = "            local _codex_retry=0"
RETRY_END = "            if [[ $_codex_retry -ge 12 && $exit_code -ne 0 ]]; then"

RETRY_HARNESS = r"""
set -u
DB="$1"; JOB="$2"; OUT="$3"
export RUNNER_CLI_HEARTBEAT_SEC=0 RUNNER_CANCEL_POLL_SEC=0; RUNNER_HOSTNAME=h
log() { echo "LOG: $*" >> "$OUT/log"; }
record_runner_event() { echo "EVENT: $2 status=$3" >> "$OUT/events"; }
_release_work_lock() { echo "RELEASE: $1 $2 $3" >> "$OUT/events"; }
_reclaim_finished_worktree() { echo "RECLAIM: $1" >> "$OUT/events"; }
db_exec() {
    python3 - "$DB" "$1" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=10)
for r in c.execute(sys.argv[2]).fetchall():
    print(r[0])
PY
}
db_update() { :; }
lookup_error_book() { :; }
mark_codex_auth_disabled() { :; }
runner_json_int_or_null() { echo null; }
runner_proc_start_ticks() { echo ""; }
runner_children_cpu_ms() { RUNNER_CPU_MS=0; }
wait_runner_cli_process() { echo "WAIT_CALLED" >> "$OUT/result"; return 0; }
codex() { echo "CODEX_LAUNCHED status=$(db_exec "SELECT status FROM pipeline_jobs WHERE job_id='$JOB'")" >> "$OUT/launched"; }
timeout() { shift; "$@"; }
sleep() {
    if [[ "$1" == "5" ]]; then
        python3 - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1], timeout=10)
c.execute("UPDATE pipeline_jobs SET status='error' WHERE job_id='runner-t1'")
c.commit()
PY
        return 0
    fi
    command sleep "$@"
}
BLOCKS
retry_loop_fn() {
    local job_id="$JOB" project=AADS parallel_group=grp attempt=0 total_attempts=2 cycle_num=1
    local current_model="codex:gpt-x" effective_model=x job_size=M workdir=/tmp
    local output_file="$OUT/o" err_file="$OUT/e" claude_pid=0 _cpu_end_ms=0 safe_instruction=i
    local _worker_nice=0 _worker_nice_applied=0 MAX_RUNTIME=60
    local -a _worker_nice_cmd=() codex_args=(exec)
    local exit_code=1
    : > "$err_file"
@@RETRY_LOOP@@
    echo "LOOP_DONE exit_code=$exit_code" >> "$OUT/result"
    return 0
}
retry_loop_fn
echo "RET=$?" >> "$OUT/result"
"""


@pytest.mark.parametrize("name", SCRIPTS)
def test_codex_retry_wait_does_not_launch_after_cancel(tmp_path, name):
    s = _read(name)
    run_job = s[s.index("\nrun_job() {") :]
    loop = run_job[run_job.index(RETRY_START) : run_job.index(RETRY_END)]
    db, out = _setup(tmp_path, "running")
    script = RETRY_HARNESS.replace("BLOCKS", _blocks(name)).replace("@@RETRY_LOOP@@", loop)
    r = subprocess.run(
        ["bash", "-c", script, "bash", str(db), "runner-t1", str(out)],
        capture_output=True, text=True, timeout=60,
    )
    assert not (out / "launched").exists(), _rd(out / "launched") + r.stderr  # 취소된 작업에 codex 를 띄우지 않았다
    result = _rd(out / "result")
    assert "WAIT_CALLED" not in result
    assert "LOOP_DONE" not in result  # 후처리·다음 모델로 흘러가지 않고 return
    assert "CANCELLED_BY_STATUS job=runner-t1 status=error" in _rd(out / "log")


@pytest.mark.parametrize("name", SCRIPTS)
def test_codex_retry_still_launches_when_running(tmp_path, name):
    s = _read(name)
    run_job = s[s.index("\nrun_job() {") :]
    loop = run_job[run_job.index(RETRY_START) : run_job.index(RETRY_END)]
    db, out = _setup(tmp_path, "running")
    script = RETRY_HARNESS.replace("BLOCKS", _blocks(name)).replace("@@RETRY_LOOP@@", loop)
    script = script.replace('if [[ "$1" == "5" ]]; then', 'if false; then')
    r = subprocess.run(
        ["bash", "-c", script, "bash", str(db), "runner-t1", str(out)],
        capture_output=True, text=True, timeout=60,
    )
    assert "CODEX_LAUNCHED status=running" in _rd(out / "launched"), r.stderr
    assert "LOOP_DONE exit_code=0" in _rd(out / "result")
