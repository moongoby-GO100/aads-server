"""러너 stuck 복구가 살아 있는 워커를 회수하지 않는다.

(AADS-RUNNER-STALE-RECOVER-LIVE-WORKER-20261007, 오류사전 runner.stale_recovered_kills_live_worker)
"""
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SH = SCRIPTS / "pipeline-runner.sh"
SRC = SH.read_text()

JOB = "runner-0123abcd"


def _function(name: str) -> str:
    lines = SRC.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _bash(script: str, timeout: int = 60):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=timeout)


def test_script_is_syntactically_valid_and_local_copy_in_sync():
    assert _bash(f"bash -n {SH}").returncode == 0
    assert SRC == (SCRIPTS / "pipeline-runner.sh.local").read_text()


def _recover(tmp_path, row_pid, row_host, peer_age="5", pid_alive=True):
    """_recover_stuck_jobs 의 stuck 구간을 돌려 UPDATE(회수) 발생 여부를 본다."""
    sql_log = tmp_path / "sql.log"
    body = _function("_recover_stuck_jobs")
    # 이 테스트 관심 구간(stuck 복구)만: 앞쪽 좀비/배포/승인 단계는 빈 결과로 흘려보낸다.
    script = f"""
set -eo pipefail
RUNNER_HOST_NAME=host-a
RUNNER_HOSTNAME=host-a
MAX_RUNTIME=7200
RS=$'\\x1e'
sql_escape() {{ printf "'%s'" "$1"; }}
log() {{ echo "LOG $*"; }}
aads_finalize_deploy_queued_jobs() {{ :; }}
post_to_chat() {{ :; }}
record_runner_event() {{ echo "EVENT $*"; }}
_notify_ai() {{ :; }}
promote_next_queued() {{ :; }}
db_update() {{ echo "$1" >> {sql_log}; }}
db_exec() {{
  echo "$1" >> {sql_log}
  case "$1" in
    *"FROM pipeline_runner_hosts"*) echo "{peer_age}" ;;
    *"SELECT job_id, COALESCE(runner_pid"*) printf '%s%s%s%s%s\\n' "{JOB}" "$RS" "{row_pid}" "$RS" "{row_host}" ;;
    *"SET status='error', phase='error',"*"error_detail='stale_recovered'"*) echo "{JOB}" ;;
    *) : ;;
  esac
}}
{_function("_stale_job_verdict")}
{body}
{'sleep 60 & live=$!' if pid_alive else 'true'}
_recover_stuck_jobs ""
{'kill $live 2>/dev/null || true' if pid_alive else 'true'}
"""
    r = _bash(script)
    assert r.returncode == 0, r.stderr + r.stdout
    return r.stdout, sql_log.read_text()


def test_alive_pid_on_this_host_is_not_recovered(tmp_path):
    out, sql = _recover(tmp_path, "$live", "host-a")
    assert f"STALE_SKIP_ALIVE job={JOB}" in out
    assert "RECOVERED stuck jobs" not in out
    assert "error_detail='stale_recovered'" not in sql
    assert f"UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='{JOB}'" in sql


def test_dead_pid_is_stale_recovered(tmp_path):
    out, sql = _recover(tmp_path, "999999", "host-a", pid_alive=False)
    assert "STALE_SKIP_ALIVE" not in out
    assert f"RECOVERED stuck jobs: {JOB}" in out
    assert "error_detail='stale_recovered'" in sql
    assert "updated_at < NOW() - INTERVAL '60 minutes'" in sql


def test_missing_pid_is_stale_recovered(tmp_path):
    out, sql = _recover(tmp_path, "", "host-a", pid_alive=False)
    assert f"RECOVERED stuck jobs: {JOB}" in out


def test_other_host_job_is_not_judged_while_peer_runner_alive(tmp_path):
    # pid 가 이 호스트에서 우연히 살아 있어도(=무의미) / 죽어 있어도 판정하지 않는다.
    out, sql = _recover(tmp_path, "999999", "host-b", peer_age="30", pid_alive=False)
    assert f"STALE_SKIP_PEER job={JOB} host=host-b" in out
    assert "RECOVERED stuck jobs" not in out
    assert "error_detail='stale_recovered'" not in sql


def test_other_host_job_is_recovered_only_when_peer_runner_is_dead(tmp_path):
    out, sql = _recover(tmp_path, "999999", "host-b", peer_age="99999", pid_alive=False)
    assert f"RECOVERED stuck jobs: {JOB}" in out
    out, sql = _recover(tmp_path, "999999", "host-b", peer_age="-1", pid_alive=False)
    assert f"RECOVERED stuck jobs: {JOB}" in out


def test_peer_lookup_failure_does_not_kill():
    script = f"""
set -eo pipefail
RUNNER_HOST_NAME=host-a
sql_escape() {{ printf "'%s'" "$1"; }}
db_exec() {{ return 1; }}
{_function("_stale_job_verdict")}
_stale_job_verdict 123 host-b
"""
    r = _bash(script)
    assert r.stdout.strip() == "skip_peer"


def test_running_job_upper_bound_is_still_owned_by_watchdog():
    body = _function("_watchdog_check")
    assert "timeout_max_runtime" in body
    assert "started_at < NOW() - INTERVAL '${MAX_JOB_RUNTIME} seconds'" in body


def _heartbeat_script(tmp_path, cli_cmd):
    touch_log = tmp_path / "touch.log"
    return f"""
set -eo pipefail
RUNNER_CLI_HEARTBEAT_SEC=1
db_update() {{ echo "$1" >> {touch_log}; }}
record_runner_event() {{ :; }}
ps() {{ [[ -r /proc/$2/stat ]] && {{ local st; st=$(</proc/$2/stat); st="${{st##*) }}"; echo "${{st%% *}}"; }} || true; }}
{_function("runner_job_touch")}
{_function("_cli_heartbeat_loop")}
{_function("wait_runner_cli_process")}
{cli_cmd}
claude_pid=$!
rc=0
wait_runner_cli_process {JOB} $claude_pid /dev/null /dev/null m m M 1 1 1 claude_cli 0 0 || rc=$?
echo "RC=$rc"
sleep 2
"""


def test_heartbeat_touches_job_while_cli_runs_and_leaves_no_loop_after_exit(tmp_path):
    # 함수는 호출 셸의 job 으로 루프를 띄운다 — 반환 뒤 이 셸의 job 이 남아 있으면 안 된다.
    script = _heartbeat_script(tmp_path, "sleep 4 &") + f"""
jobs -p > {tmp_path}/jobs.txt
echo "LEFT=$(wc -l < {tmp_path}/jobs.txt)"
"""
    r = _bash(script)
    assert r.returncode == 0, r.stderr
    assert "RC=0" in r.stdout
    touches = (tmp_path / "touch.log").read_text()
    assert f"UPDATE pipeline_jobs SET updated_at=NOW() WHERE job_id='{JOB}' AND status='running'" in touches
    assert touches.count("updated_at=NOW()") >= 2
    left = int(r.stdout.split("LEFT=")[1].split()[0])
    assert left == 0, r.stdout


def test_heartbeat_loop_exits_when_owner_shell_dies(tmp_path):
    pid_file = tmp_path / "loop.pid"
    state_file = tmp_path / "loop.state"
    script = f"""
RUNNER_CLI_HEARTBEAT_SEC=1
db_update() {{ :; }}
{_function("runner_job_touch")}
{_function("_cli_heartbeat_loop")}
sleep 60 & cli=$!
( owner=$BASHPID; _cli_heartbeat_loop {JOB} $cli $owner & echo $! > {pid_file}; sleep 1 )
# 소유 서브셸은 끝났고 CLI 는 아직 살아 있다 — 루프는 따라서 종료해야 한다.
sleep 3
kill -0 $cli && echo CLI_ALIVE
st=$(</proc/$(cat {pid_file})/stat) || st=") X"
echo "${{st##*) }}" > {state_file}
kill $cli 2>/dev/null || true
"""
    r = _bash(script)
    assert r.returncode == 0, r.stderr
    assert "CLI_ALIVE" in r.stdout
    state = state_file.read_text().split()[0]
    assert state in ("X", "Z"), f"소유 셸이 죽은 뒤에도 heartbeat 루프가 남았다(state={state})"


def test_heartbeat_can_be_disabled(tmp_path):
    script = _heartbeat_script(tmp_path, "sleep 2 &").replace(
        "RUNNER_CLI_HEARTBEAT_SEC=1", "RUNNER_CLI_HEARTBEAT_SEC=0"
    )
    r = _bash(script)
    assert r.returncode == 0, r.stderr
    assert "RC=0" in r.stdout
    assert not (tmp_path / "touch.log").exists()
