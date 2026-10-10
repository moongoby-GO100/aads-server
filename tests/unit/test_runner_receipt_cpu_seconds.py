"""러너 실행 위치 receipt · 작업별 CPU-seconds · 우선순위 nice.

(AADS-RUNNER-LR01-RECEIPT-20261010, PRD R2 LR01/LR03)
"""
import json
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SH = SCRIPTS / "pipeline-runner.sh"
SRC = SH.read_text()

HELPERS = (
    "_receipt_json_str",
    "runner_proc_start_ticks",
    "runner_cgroup_path",
    "runner_db_target",
    "runner_children_cpu_ms",
    "runner_cpu_seconds_json",
    "runner_json_int_or_null",
    "runner_worker_nice_value",
    "runner_job_receipt_fields",
)

RECEIPT_KEYS = {
    "control_host", "executor_host", "runner_pid", "runner_start_ticks", "cgroup",
    "source_sha", "runner_script_sha256", "db_target", "queue_wait_s", "nice",
}


def _function(name: str) -> str:
    lines = SRC.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _bash(body: str, env: dict | None = None):
    script = "set -eo pipefail\n" + "\n".join(_function(n) for n in HELPERS) + "\n" + body
    import os

    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {})},
    )


def test_script_is_valid_and_local_copy_in_sync():
    assert subprocess.run(["bash", "-n", str(SH)]).returncode == 0
    assert SRC == (SCRIPTS / "pipeline-runner.sh.local").read_text()


def test_receipt_fields_present_and_valid_json():
    r = _bash(
        'RUNNER_HOST_NAME=ctl-1; RUNNER_HOSTNAME=exec-1; RUNNER_SELF_FINGERPRINT=abc123\n'
        'DB_MODE=psql; PGHOST=db.example; PGPORT=5433\n'
        'me=$BASHPID\n'
        'echo "{$(runner_job_receipt_fields "$me" deadbeef 10 42)}"\n',
        env={"PGPASSWORD": "super-secret-pw", "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-xxx"},
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert RECEIPT_KEYS <= set(data)
    assert data["control_host"] == "ctl-1" and data["executor_host"] == "exec-1"
    assert data["source_sha"] == "deadbeef" and data["runner_script_sha256"] == "abc123"
    assert data["db_target"] == "db.example:5433"
    assert data["queue_wait_s"] == 42 and data["nice"] == 10
    assert isinstance(data["runner_pid"], int)
    assert isinstance(data["runner_start_ticks"], int)  # /proc/<pid>/stat 22번째
    assert "super-secret-pw" not in r.stdout and "sk-ant" not in r.stdout


def test_receipt_missing_values_become_null_not_failure():
    r = _bash(
        'RUNNER_HOST_NAME=c; RUNNER_HOSTNAME=e; DB_MODE=docker; PG_CONTAINER=aads-postgres\n'
        'echo "{$(runner_job_receipt_fields 999999999 "" 0 "")}"\n'
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["queue_wait_s"] is None and data["runner_start_ticks"] is None
    assert data["db_target"] == "docker-exec:aads-postgres"


def test_start_ticks_handles_comm_with_spaces_and_parens(tmp_path):
    r = _bash(
        'exec -a "x (y) z" sleep 5 & p=$!; sleep 0.3\n'
        'runner_proc_start_ticks "$p"; echo; kill $p 2>/dev/null || true\n'
    )
    assert r.stdout.strip().isdigit(), (r.stdout, r.stderr)


def test_cpu_seconds_measured_for_waited_children():
    r = _bash(
        'ARTIFACT_DIR=$(mktemp -d)\n'
        'runner_children_cpu_ms; s="$RUNNER_CPU_MS"\n'
        'python3 -c "import time\nt=time.process_time()\nwhile time.process_time()-t<0.4: pass" &\n'
        'wait $!\n'
        'runner_children_cpu_ms; e="$RUNNER_CPU_MS"\n'
        'runner_cpu_seconds_json "$s" "$e"\n'
    )
    assert r.returncode == 0, r.stderr
    assert 0.3 <= float(r.stdout.strip()) < 3.0, r.stdout


def test_cpu_measurement_failure_is_null_and_does_not_abort():
    r = _bash(
        'ARTIFACT_DIR=$(mktemp -d)\n'
        'times() { return 1; }\n'
        'runner_children_cpu_ms; s="$RUNNER_CPU_MS"\n'
        'times() { echo garbage; echo nonsense; }\n'
        'runner_children_cpu_ms; e="$RUNNER_CPU_MS"\n'
        'echo "s=[$s] e=[$e] json=$(runner_cpu_seconds_json "$s" "$e") rev=$(runner_cpu_seconds_json 5000 1000)"\n'
        'echo still-running\n'
    )
    assert r.returncode == 0, r.stderr
    assert "s=[] e=[] json=null rev=null" in r.stdout
    assert "still-running" in r.stdout


def test_nice_p2_is_10_and_p0_p1_are_0():
    def nice(instr: str, env: str = "") -> str:
        r = _bash(f'{env}\nrunner_worker_nice_value "$1"\n'.replace('"$1"', f"'{instr}'"))
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    assert nice("TASK_ID: X\nPRIORITY: P2\nSIZE: M") == "10"
    assert nice("TASK_ID: X\nPRIORITY: P3") == "10"
    assert nice("no priority line at all") == "10"
    assert nice("TASK_ID: X\nPRIORITY: P1\nSIZE: M") == "0"
    assert nice("TASK_ID: X\nPRIORITY: P0\nSIZE: M") == "0"
    assert nice("PRIORITY:P0") == "0"
    assert nice("PRIORITY: P2", "RUNNER_LOW_PRIORITY_NICE=0") == "0"
    assert nice("PRIORITY: P2", "RUNNER_LOW_PRIORITY_NICE=5") == "5"
    assert nice("PRIORITY: P2", "RUNNER_LOW_PRIORITY_NICE=abc") == "10"
    assert nice("PRIORITY: P2", "RUNNER_LOW_PRIORITY_NICE=99") == "19"


def test_nice_prefix_really_changes_worker_priority():
    r = _bash(
        'for instr in "PRIORITY: P2" "PRIORITY: P1"; do\n'
        '  n=$(runner_worker_nice_value "$instr"); cmd=(); (( n > 0 )) && cmd=(nice -n "$n")\n'
        '  ${cmd[@]+"${cmd[@]}"} sh -c "nice" \n'
        'done\n'
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["10", "0"]


def test_every_host_worker_launch_uses_nice_prefix_and_job_started_has_receipt():
    prefix = '${_worker_nice_cmd[@]+"${_worker_nice_cmd[@]}"}'
    lines = SRC.splitlines()
    launch_markers = (
        'timeout "$MAX_RUNTIME" codex ',
        'timeout "$MAX_RUNTIME" "$litellm_python" ',
        'timeout "$MAX_RUNTIME" "$RUNNER_CLAUDE_CLI_BIN" ',
    )
    hits = [i for i, l in enumerate(lines) if any(m in l for m in launch_markers)]
    assert len(hits) == 5  # codex x2, litellm host, claude x2 (slot 분기는 nice 가 env 바깥에서 감싼다)
    slot_idx = next(i for i, l in enumerate(lines) if 'env -u CLAUDE_CODE_OAUTH_TOKEN' in l)
    assert prefix in lines[slot_idx - 1]
    for i in hits:
        if i < slot_idx or i > slot_idx + 6:
            assert prefix in lines[i] or prefix in lines[i - 1], lines[i]
    started = next(l for l in lines if '"job_started"' in l)
    assert "runner_job_receipt_fields" in started
    done = next(l for l in lines if '"model_attempt_completed"' in l)
    for key in ("cpu_seconds", "run_s", "executor_host", "control_host"):
        assert f'\\"{key}\\"' in done
    assert "RUNNER_LOW_PRIORITY_NICE" in SRC
