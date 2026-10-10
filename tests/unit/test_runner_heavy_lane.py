"""서버별 무거운 명령 lane · 긴급 예약 슬롯 · runner_host_policy.

(AADS-RUNNER-LR04-HEAVY-LANE-20261010, PRD R2 LR03/LR04)
"""
import json
import os
import subprocess
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ROOT = SCRIPTS.parent
SH = SCRIPTS / "pipeline-runner.sh"
SRC = SH.read_text()
MIGRATION = (ROOT / "migrations/20261010_runner_host_policy.sql").read_text()

POLICY_FUNCS = (
    "runner_policy_int_in_range",
    "runner_policy_warn_once",
    "runner_host_policy_apply",
    "runner_host_policy_refresh",
    "runner_heavy_publish_policy",
    "runner_heavy_install_shims",
    "runner_heavy_job_env",
    "runner_heavy_flush_events",
    "runner_cpu_context_json",
    "_receipt_json_str",
)


def _function(name: str) -> str:
    lines = SRC.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _bash(body: str, env: dict | None = None, timeout: int = 60):
    script = "set -eo pipefail\n" + "\n".join(_function(n) for n in POLICY_FUNCS) + "\n" + body
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )


# ── 공통 픽스처 ──────────────────────────────────────────────────────

def _install_lane(tmp_path: Path) -> Path:
    lane = tmp_path / "lane"
    r = _bash(f'RUNNER_HEAVY_DIR={lane}\nrunner_heavy_install_shims\n')
    assert r.returncode == 0, r.stderr
    return lane


def _write_policy(lane: Path, slots: int, urgent: int = 0, revision: int = 1):
    (lane / "policy").write_text(f"slots={slots}\nurgent={urgent}\nrevision={revision}\n")


def _real_bin(tmp_path: Path) -> Path:
    real = tmp_path / "real"
    real.mkdir()
    for name in ("pytest", "npm", "npx", "node", "tsc", "next", "bash"):
        f = real / name
        f.write_text(f'#!/bin/sh\necho "REAL {name} HELD=${{AADS_HEAVY_HELD-unset}} ARGS=$*"\n')
        f.chmod(0o755)
    return real


def _shim_env(lane: Path, real: Path, **extra) -> dict:
    env = {
        "PATH": f"{lane}/bin:{real}:/usr/bin:/bin",
        "AADS_HEAVY_LANE_DIR": str(lane),
        "AADS_HEAVY_POLL_SEC": "0.1",
        "HOME": str(lane.parent),
    }
    env.update(extra)
    return env


def _run(cmd, env, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _hold_slot(lane: Path, idx: int, seconds: int = 20) -> subprocess.Popen:
    slot = lane / f"slot-{idx}"
    slot.touch()
    p = subprocess.Popen(["flock", "-x", str(slot), "sleep", str(seconds)])
    deadline = time.time() + 5
    while time.time() < deadline:
        probe = subprocess.run(["flock", "-n", str(slot), "true"], capture_output=True)
        if probe.returncode != 0:
            return p
        time.sleep(0.05)
    p.kill()
    raise AssertionError("slot holder did not acquire the lock")


def _events(lane: Path, job="nojob"):
    f = lane / "events" / f"{job}.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        ev, meta = line.split("\t", 1)
        out.append((ev, json.loads(meta)))
    return out


# ── 스크립트 일반 ────────────────────────────────────────────────────

def test_script_is_valid_and_local_copy_in_sync():
    assert subprocess.run(["bash", "-n", str(SH)]).returncode == 0
    assert SRC == (SCRIPTS / "pipeline-runner.sh.local").read_text()


def test_wired_into_main_loop_and_run_job_without_touching_claim():
    main = _function("main")
    assert "runner_host_policy_refresh" in main and "runner_heavy_flush_events" in main
    assert main.index("runner_maintenance_checkpoint_or_hold") < main.index("runner_host_policy_refresh")
    assert main.index("runner_host_policy_refresh") < main.index("claim_queued_job")
    assert "runner_heavy_job_env" in _function("run_job")
    claim = SRC.split("claim_queued_job() (\n", 1)[1].split("\n)\n", 1)[0]
    assert "MAX_CONCURRENT_SERVER" in claim and "runner_host_policy" not in claim


def test_shim_body_has_no_column_zero_brace():
    # 함수 추출 테스트(_function)가 heredoc 안의 '}' 에서 끊기지 않아야 한다.
    body = _function("runner_heavy_install_shims")
    assert body.splitlines()[-1] == "}"
    assert sum(1 for l in body.splitlines() if l == "}") == 1


# ── 슬롯 0 / 정책 없음 → 통과 ────────────────────────────────────────

def test_no_policy_file_is_pure_passthrough(tmp_path):
    lane = _install_lane(tmp_path)
    real = _real_bin(tmp_path)
    env = _shim_env(lane, real)
    r = _run([str(lane / "bin/pytest"), "-q", "tests"], env)
    assert r.returncode == 0
    assert r.stdout.strip() == "REAL pytest HELD=unset ARGS=-q tests"
    assert not list(lane.glob("slot-*"))
    assert _events(lane) == []


def test_zero_or_empty_slots_is_passthrough(tmp_path):
    lane = _install_lane(tmp_path)
    real = _real_bin(tmp_path)
    env = _shim_env(lane, real)
    for content in ("slots=0\nurgent=0\n", "slots=\n", "garbage\n", "slots=abc\n"):
        (lane / "policy").write_text(content)
        r = _run([str(lane / "bin/pytest")], env)
        assert r.stdout.strip() == "REAL pytest HELD=unset ARGS=", content
    assert not list(lane.glob("slot-*"))


def test_shim_install_is_idempotent_and_does_not_shadow_itself(tmp_path):
    lane = _install_lane(tmp_path)
    first = (lane / "bin/.aads-heavy-shim").read_text()
    _install_lane(tmp_path)
    assert (lane / "bin/.aads-heavy-shim").read_text() == first
    for name in ("pytest", "npm", "npx", "node", "tsc", "next", "bash"):
        assert os.readlink(lane / "bin" / name) == ".aads-heavy-shim"
    # 실제 명령이 없으면 자기 자신을 다시 부르지 않고 127
    env = _shim_env(lane, tmp_path / "empty")
    env["PATH"] = f"{lane}/bin:/nonexistent"
    r = _run([str(lane / "bin/pytest")], env)
    assert r.returncode == 127


# ── 슬롯 1: 두 명령 직렬화 ───────────────────────────────────────────

def test_one_slot_serializes_two_commands(tmp_path):
    lane = _install_lane(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    log = tmp_path / "order.log"
    pytest = real / "pytest"
    pytest.write_text(
        f'#!/bin/sh\necho "start $1" >> {log}\nsleep 1\necho "end $1" >> {log}\n'
    )
    pytest.chmod(0o755)
    _write_policy(lane, slots=1)
    env = _shim_env(lane, real, AADS_HEAVY_JOB_ID="runner-t1")
    p1 = subprocess.Popen([str(lane / "bin/pytest"), "A"], env=env)
    time.sleep(0.3)
    p2 = subprocess.Popen([str(lane / "bin/pytest"), "B"], env=env)
    assert p1.wait(30) == 0 and p2.wait(30) == 0
    assert log.read_text().split("\n")[:-1] == ["start A", "end A", "start B", "end B"]
    names = [e for e, _ in _events(lane, "runner-t1")]
    assert "heavy_lane_wait" in names and "heavy_lane_acquired" in names


def test_two_slots_run_in_parallel(tmp_path):
    lane = _install_lane(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    log = tmp_path / "order.log"
    pytest = real / "pytest"
    pytest.write_text(f'#!/bin/sh\necho "start $1" >> {log}\nsleep 1\necho "end $1" >> {log}\n')
    pytest.chmod(0o755)
    _write_policy(lane, slots=2)
    env = _shim_env(lane, real)
    p1 = subprocess.Popen([str(lane / "bin/pytest"), "A"], env=env)
    p2 = subprocess.Popen([str(lane / "bin/pytest"), "B"], env=env)
    assert p1.wait(30) == 0 and p2.wait(30) == 0
    lines = log.read_text().split("\n")[:-1]
    assert lines[:2] in (["start A", "start B"], ["start B", "start A"])


def test_stdin_exit_code_and_args_survive_exec(tmp_path):
    lane = _install_lane(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    f = real / "pytest"
    f.write_text('#!/bin/sh\ncat\nexit 7\n')
    f.chmod(0o755)
    _write_policy(lane, slots=1)
    r = subprocess.run([str(lane / "bin/pytest")], input="hello", capture_output=True, text=True,
                       env=_shim_env(lane, real), timeout=30)
    assert r.stdout == "hello" and r.returncode == 7


# ── 긴급 예약 슬롯 / 대기 상한 ───────────────────────────────────────

def test_urgent_reserved_slot_is_unusable_by_normal_jobs(tmp_path):
    lane = _install_lane(tmp_path)
    real = _real_bin(tmp_path)
    _write_policy(lane, slots=2, urgent=1, revision=9)
    holder = _hold_slot(lane, 0)  # 일반 슬롯(0)을 다른 명령이 쥐고 있다
    try:
        # 일반 작업: 슬롯 1 은 비어 있어도 못 쓴다 → 상한(2s) 후 슬롯 없이 실행
        env = _shim_env(lane, real, AADS_HEAVY_MAX_WAIT_SEC="2", AADS_HEAVY_JOB_ID="runner-norm",
                        AADS_HEAVY_URGENT="0")
        t0 = time.time()
        r = _run([str(lane / "bin/pytest")], env)
        assert time.time() - t0 >= 1.0  # 대기 시간은 정수 초 단위로 센다
        assert "HELD=bypass" in r.stdout
        assert "대기 후에도 heavy 슬롯을 못 잡아" in r.stderr
        # 긴급 작업: 같은 순간 예약 슬롯(1)을 즉시 쓴다
        env_u = _shim_env(lane, real, AADS_HEAVY_MAX_WAIT_SEC="30", AADS_HEAVY_JOB_ID="runner-urg",
                          AADS_HEAVY_URGENT="1")
        t0 = time.time()
        r = _run([str(lane / "bin/pytest")], env_u)
        assert time.time() - t0 < 1.0
        assert "HELD=slot-1" in r.stdout
        assert _events(lane, "runner-urg") == []
    finally:
        holder.kill()
        holder.wait()


def test_urgent_prefers_reserved_slot_then_general(tmp_path):
    lane = _install_lane(tmp_path)
    real = _real_bin(tmp_path)
    _write_policy(lane, slots=3, urgent=1)
    env = _shim_env(lane, real, AADS_HEAVY_URGENT="1")
    assert "HELD=slot-2" in _run([str(lane / "bin/pytest")], env).stdout
    holder = _hold_slot(lane, 2)
    try:
        assert "HELD=slot-1" in _run([str(lane / "bin/pytest")], env).stdout
    finally:
        holder.kill()
        holder.wait()
    env_n = _shim_env(lane, real, AADS_HEAVY_URGENT="0")
    assert "HELD=slot-0" in _run([str(lane / "bin/pytest")], env_n).stdout


def test_wait_cap_logs_event_with_wait_reason_and_next_check(tmp_path):
    lane = _install_lane(tmp_path)
    real = _real_bin(tmp_path)
    _write_policy(lane, slots=1, revision=4)
    holder = _hold_slot(lane, 0)
    try:
        env = _shim_env(lane, real, AADS_HEAVY_MAX_WAIT_SEC="2", AADS_HEAVY_POLL_SEC="1",
                        AADS_HEAVY_JOB_ID="runner-cap")
        t0 = time.time()
        r = _run([str(lane / "bin/pytest")], env)
        assert 1.0 <= time.time() - t0 < 10
        assert r.returncode == 0 and "HELD=bypass" in r.stdout
    finally:
        holder.kill()
        holder.wait()
    events = _events(lane, "runner-cap")
    kinds = [e for e, _ in events]
    assert kinds[0] == "heavy_lane_wait" and kinds[-1] == "heavy_lane_wait_timeout"
    first = events[0][1]
    assert first["wait_reason"] == "heavy_slot_busy"
    assert first["command"] == "pytest" and first["policy_revision"] == 4
    assert first["next_check_at"].endswith("Z") and first["heavy_slots"] == 1
    assert events[-1][1]["waited_s"] >= 2 and events[-1][1]["max_wait_s"] == 2


def test_default_wait_cap_is_twenty_minutes():
    assert "AADS_HEAVY_MAX_WAIT_SEC:-1200" in _function("runner_heavy_install_shims")


def test_nested_command_inside_held_slot_does_not_wait(tmp_path):
    lane = _install_lane(tmp_path)
    real = _real_bin(tmp_path)
    _write_policy(lane, slots=1)
    holder = _hold_slot(lane, 0)
    try:
        env = _shim_env(lane, real, AADS_HEAVY_MAX_WAIT_SEC="30", AADS_HEAVY_HELD="slot-0")
        t0 = time.time()
        r = _run([str(lane / "bin/pytest")], env)
        assert time.time() - t0 < 1.0
        assert "HELD=slot-0" in r.stdout
    finally:
        holder.kill()
        holder.wait()


def test_slot_is_released_when_command_exits(tmp_path):
    lane = _install_lane(tmp_path)
    real = _real_bin(tmp_path)
    _write_policy(lane, slots=1)
    env = _shim_env(lane, real)
    _run([str(lane / "bin/pytest")], env)
    probe = subprocess.run(["flock", "-n", str(lane / "slot-0"), "true"])
    assert probe.returncode == 0


def test_lock_is_held_by_the_exec_ed_command(tmp_path):
    lane = _install_lane(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    f = real / "pytest"
    f.write_text(f'#!/bin/sh\nflock -n {lane}/slot-0 true && echo FREE || echo BUSY\n')
    f.chmod(0o755)
    _write_policy(lane, slots=1)
    r = _run([str(lane / "bin/pytest")], _shim_env(lane, real))
    assert r.stdout.strip() == "BUSY"


# ── 어떤 명령이 무거운가 ─────────────────────────────────────────────

def test_heavy_command_classification(tmp_path):
    lane = _install_lane(tmp_path)
    real = _real_bin(tmp_path)
    _write_policy(lane, slots=1)
    holder = _hold_slot(lane, 0)
    env = _shim_env(lane, real, AADS_HEAVY_MAX_WAIT_SEC="1")
    heavy = [
        ("pytest", ["-q"]), ("tsc", ["--noEmit"]), ("next", ["build"]), ("npx", ["tsc"]),
        ("npm", ["run", "build"]), ("npm", ["test"]), ("npm", ["--prefix", "web", "ci"]),
        ("node", ["node_modules/next/dist/bin/next", "build"]),
        ("node", ["node_modules/typescript/bin/tsc", "-p", "."]),
        ("bash", ["scripts/run_unit_tests.sh", "tests/unit/x.py"]),
        ("bash", ["-e", "scripts/run_unit_tests.sh"]),
    ]
    light = [
        ("npm", ["--version"]), ("npm", ["config", "get", "registry"]), ("npm", []),
        ("node", ["server.js"]), ("node", ["-e", "1"]),
        ("node", ["node_modules/next/dist/bin/next", "dev"]),
        ("bash", ["-c", "echo hi"]), ("bash", ["scripts/deploy.sh"]),
    ]
    try:
        for name, args in heavy:
            r = _run([str(lane / "bin" / name), *args], env)
            assert "HELD=bypass" in r.stdout, (name, args, r.stdout)
        for name, args in light:
            t0 = time.time()
            r = _run([str(lane / "bin" / name), *args], env)
            assert "HELD=unset" in r.stdout and time.time() - t0 < 1.0, (name, args, r.stdout)
    finally:
        holder.kill()
        holder.wait()


# ── 정책 읽기: env 폴백 · 범위 · 마지막 값 ───────────────────────────

def _policy_script(lane: Path, steps: str, env_lines: str = "MAX_CONCURRENT_SERVER=20") -> str:
    return f"""
RUNNER_HEAVY_DIR={lane}
RUNNER_POLICY_REFRESH_SEC=0
RUNNER_HOST_NAME=host-a
{env_lines}
STATE={lane}.state
mkdir -p "$STATE"
log() {{ echo "LOG $*" >&2; }}
sql_escape() {{ echo "'$1'"; }}
db_exec() {{
  [[ -f "$STATE/fail" ]] && return 1
  case "$1" in
    *to_regclass*) cat "$STATE/ready" ;;
    *) cat "$STATE/row" ;;
  esac
}}
set_row() {{ printf 't' > "$STATE/ready"; printf "$1" > "$STATE/row"; }}
show() {{ echo "SERVER=${{MAX_CONCURRENT_SERVER:-}} GLOBAL=${{MAX_CONCURRENT_GLOBAL:-unset}} NICE=${{RUNNER_LOW_PRIORITY_NICE:-unset}}"; }}
{steps}
"""


def test_no_row_keeps_env_behaviour(tmp_path):
    lane = tmp_path / "lane"
    r = _bash(_policy_script(lane, 'set_row ""\nrunner_host_policy_refresh\nshow\n'))
    assert r.returncode == 0, r.stderr
    assert "SERVER=20 GLOBAL=unset NICE=unset" in r.stdout
    assert not (lane / "policy").exists()
    assert "HOST_POLICY_APPLIED" not in r.stderr


def test_missing_table_and_query_failure_fall_back_to_env(tmp_path):
    lane = tmp_path / "lane"
    steps = (
        'printf f > "$STATE/ready"; : > "$STATE/row"\nrunner_host_policy_refresh\nshow\n'
        'touch "$STATE/fail"\nrunner_host_policy_refresh\nshow\n'
    )
    r = _bash(_policy_script(lane, steps))
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("SERVER=20 GLOBAL=unset") == 2
    assert "HOST_POLICY_READ_FAILED" in r.stderr


def test_row_applies_without_restart_and_failure_keeps_last_value(tmp_path):
    lane = tmp_path / "lane"
    steps = (
        "set_row '7\\x1e12\\x1e2\\x1e1\\x1e5\\n'\nrunner_host_policy_refresh\nshow\n"
        'touch "$STATE/fail"\nrunner_host_policy_refresh\nshow\n'
        'rm "$STATE/fail"\nset_row "8\\x1e30\\x1e2\\x1e1\\x1e"\nrunner_host_policy_refresh\nshow\n'
    )
    r = _bash(_policy_script(lane, steps))
    assert r.returncode == 0, r.stderr
    lines = [l for l in r.stdout.splitlines() if l.startswith("SERVER=")]
    assert lines == [
        "SERVER=12 GLOBAL=unset NICE=5",
        "SERVER=12 GLOBAL=unset NICE=5",  # 조회 실패 → 마지막 값
        "SERVER=30 GLOBAL=unset NICE=unset",  # 새 revision, nice 비우면 env(unset)
    ]
    assert (lane / "policy").read_text() == "slots=2\nurgent=1\nrevision=8\n"
    assert "HOST_POLICY_APPLIED host=host-a revision=7 max_concurrent=12" in r.stderr


def test_out_of_range_values_are_ignored_and_logged(tmp_path):
    lane = tmp_path / "lane"
    for bad in ("0", "201", "999999", "-3", "abc"):
        steps = f"set_row '3\\x1e{bad}\\x1e\\x1e\\x1e\\n'\nrunner_host_policy_refresh\nshow\n"
        r = _bash(_policy_script(lane, steps))
        assert r.returncode == 0, r.stderr
        assert "SERVER=20 GLOBAL=unset" in r.stdout, bad
        assert "HOST_POLICY_IGNORED" in r.stderr and "field=max_concurrent" in r.stderr
    # 경계값은 받아들인다
    for ok in ("1", "200"):
        r = _bash(_policy_script(lane, f"set_row '3\\x1e{ok}\\x1e\\x1e\\x1e\\n'\nrunner_host_policy_refresh\nshow\n"))
        assert f"SERVER={ok} " in r.stdout


def test_removed_row_reverts_to_env_and_removes_policy_file(tmp_path):
    lane = tmp_path / "lane"
    steps = (
        "set_row '2\\x1e5\\x1e3\\x1e1\\x1e\\n'\nrunner_host_policy_refresh\nshow\n"
        'test -f "$RUNNER_HEAVY_DIR/policy" && echo POLICY_FILE || true\n'
        "set_row ''\nrunner_host_policy_refresh\nshow\n"
        'test -f "$RUNNER_HEAVY_DIR/policy" && echo POLICY_FILE_STILL || true\n'
    )
    r = _bash(_policy_script(lane, steps))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        "SERVER=5 GLOBAL=unset NICE=unset", "POLICY_FILE", "SERVER=20 GLOBAL=unset NICE=unset",
    ]
    assert "HOST_POLICY_CLEARED" in r.stderr


def test_stale_policy_file_from_previous_run_is_cleared_when_no_row(tmp_path):
    lane = tmp_path / "lane"
    lane.mkdir()
    (lane / "policy").write_text("slots=2\nurgent=0\nrevision=1\n")
    r = _bash(_policy_script(lane, 'set_row ""\nrunner_host_policy_refresh\n'))
    assert r.returncode == 0, r.stderr
    assert not (lane / "policy").exists()


def test_zero_heavy_slots_publishes_no_policy(tmp_path):
    lane = tmp_path / "lane"
    for heavy in ("0", ""):
        r = _bash(_policy_script(lane, f"set_row '2\\x1e10\\x1e{heavy}\\x1e\\x1e\\n'\nrunner_host_policy_refresh\nshow\n"))
        assert r.returncode == 0, r.stderr
        assert "SERVER=10" in r.stdout
        assert not (lane / "policy").exists()


def test_legacy_global_host_replaces_global_limit_not_mode(tmp_path):
    lane = tmp_path / "lane"
    r = _bash(
        _policy_script(lane, "set_row '2\\x1e12\\x1e\\x1e\\x1e\\n'\nrunner_host_policy_refresh\nshow\n",
                       env_lines="MAX_CONCURRENT_SERVER=\nMAX_CONCURRENT_GLOBAL=10"),
    )
    assert r.returncode == 0, r.stderr
    assert "SERVER= GLOBAL=12" in r.stdout


def test_urgent_reserved_is_clamped_to_heavy_slots(tmp_path):
    lane = tmp_path / "lane"
    r = _bash(_policy_script(lane, "set_row '2\\x1e\\x1e2\\x1e5\\x1e\\n'\nrunner_host_policy_refresh\n"))
    assert r.returncode == 0, r.stderr
    assert (lane / "policy").read_text() == "slots=2\nurgent=2\nrevision=2\n"
    assert "HOST_POLICY_WARN" in r.stderr


def test_refresh_is_throttled(tmp_path):
    lane = tmp_path / "lane"
    steps = (
        "RUNNER_POLICY_REFRESH_SEC=3600\n"
        "set_row '2\\x1e5\\x1e\\x1e\\x1e\\n'\nrunner_host_policy_refresh\nshow\n"
        "set_row '3\\x1e9\\x1e\\x1e\\x1e\\n'\nrunner_host_policy_refresh\nshow\n"
    )
    r = _bash(_policy_script(lane, steps))
    assert [l for l in r.stdout.splitlines() if l.startswith("SERVER=")] == ["SERVER=5 GLOBAL=unset NICE=unset"] * 2


# ── 작업 환경 ────────────────────────────────────────────────────────

def test_job_env_untouched_without_policy(tmp_path):
    lane = tmp_path / "lane"
    r = _bash(
        f'RUNNER_HEAVY_DIR={lane}\nbefore="$PATH"\nrunner_heavy_job_env "PRIORITY: P0" runner-x\n'
        '[[ "$PATH" == "$before" ]] && echo SAME\nenv | grep -c "^AADS_HEAVY" || true\n'
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["SAME", "0"]


def test_job_env_prepends_shim_dir_and_marks_urgency(tmp_path):
    lane = _install_lane(tmp_path)
    _write_policy(lane, slots=2)
    cases = {"PRIORITY: P0\nx": "1", "PRIORITY:P1": "1", "PRIORITY: P2": "0", "title P0 only": "0", "PRIORITY: P10": "0"}
    for instr, want in cases.items():
        r = subprocess.run(
            ["bash", "-c",
             "set -eo pipefail\n" + _function("runner_heavy_job_env") + "\n"
             f'RUNNER_HEAVY_DIR={lane}\nrunner_heavy_job_env "$1" runner-abc\n'
             'echo "${PATH%%:*} $AADS_HEAVY_URGENT $AADS_HEAVY_JOB_ID"\n'
             'runner_heavy_job_env "$1" runner-abc\n'
             f'echo "$PATH" | tr : "\\n" | grep -cx "{lane}/bin"\n', "x", instr],
            capture_output=True, text=True, timeout=30,
        ).stdout
        first, count = r.strip().splitlines()
        assert first == f"{lane}/bin {want} runner-abc", (instr, r)
        assert count == "1"  # 두 번 호출해도 PATH 에 한 번만


# ── 대기 이벤트 flush ────────────────────────────────────────────────

def test_flush_moves_shim_events_to_runner_events_with_cpu_context(tmp_path):
    lane = tmp_path / "lane"
    (lane / "events").mkdir(parents=True)
    meta = '{"event":"heavy_lane_wait","wait_reason":"heavy_slot_busy","waited_s":0,"next_check_at":"2026-10-10T00:00:02Z"}'
    (lane / "events/runner-ev1.jsonl").write_text(
        f"heavy_lane_wait\t{meta}\nbogus line\nrm -rf\t{{}}\nheavy_lane_wait_timeout\t{meta}\n"
    )
    out = tmp_path / "events.out"
    r = _bash(
        f'RUNNER_HEAVY_DIR={lane}\nRUNNER_HOST_NAME=host-a\nlog() {{ echo "LOG $*" >&2; }}\n'
        'runner_cpu_context_json() { echo \'{"snapshot_status":"fresh","cpu_valid":false}\'; }\n'
        f'record_runner_event() {{ printf "%s|%s|%s\\n" "$1" "$2" "$9" >> {out}; }}\n'
        "runner_heavy_flush_events\n"
    )
    assert r.returncode == 0, r.stderr
    rows = out.read_text().splitlines()
    assert len(rows) == 2
    job, ev, payload = rows[0].split("|", 2)
    assert (job, ev) == ("runner-ev1", "heavy_lane_wait")
    data = json.loads(payload)
    assert data["wait_reason"] == "heavy_slot_busy" and data["next_check_at"]
    assert data["cpu_context"] == {"snapshot_status": "fresh", "cpu_valid": False}
    assert rows[1].split("|")[1] == "heavy_lane_wait_timeout"
    assert "HEAVY_LANE_WAIT_TIMEOUT job=runner-ev1" in r.stderr
    assert not list((lane / "events").glob("*"))
    # 두 번째 호출은 아무것도 하지 않는다
    r2 = _bash(
        f'RUNNER_HEAVY_DIR={lane}\nrecord_runner_event() {{ echo CALLED; }}\nrunner_heavy_flush_events\n'
    )
    assert r2.returncode == 0 and "CALLED" not in r2.stdout


def test_flush_without_events_dir_is_noop(tmp_path):
    r = _bash(f'RUNNER_HEAVY_DIR={tmp_path}/none\nrunner_heavy_flush_events\n')
    assert r.returncode == 0 and r.stdout == ""


def test_cpu_context_reports_unavailable_without_blocking(tmp_path):
    r = _bash("runner_cpu_context_json\n", env={"AADS_CPU_BIN": str(tmp_path / "missing")})
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.splitlines()[-1])
    assert d["snapshot_status"] == "unavailable" and d["cpu_valid"] is None
    assert "runner_cpu_pressure" in d


def test_cpu_context_reads_snapshot_freshness_and_validity(tmp_path):
    fake = tmp_path / "aads-cpu"
    fake.write_text(
        "#!/bin/sh\n"
        'echo \'{"status":"stale","sample_age_s":93.5,"sample":{"cpu":{"valid":false},"psi":{"cpu":{"some":{"avg10":41.0}}}}}\'\n'
    )
    fake.chmod(0o755)
    r = _bash("runner_cpu_context_json\n", env={"AADS_CPU_BIN": str(fake)})
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.splitlines()[-1])
    assert d["snapshot_status"] == "stale" and d["snapshot_age_s"] == 93.5
    assert d["cpu_valid"] is False and d["host_psi_cpu_some_avg10"] == 41.0


# ── 마이그레이션 ─────────────────────────────────────────────────────

def test_migration_policy_table_and_audit_shape():
    for col in ("host                  text PRIMARY KEY", "max_concurrent", "heavy_slots", "urgent_reserved_slots",
                "low_priority_nice", "revision              int NOT NULL DEFAULT 1", "updated_by", "updated_at", "note"):
        assert col in MIGRATION
    assert "max_concurrent IS NULL OR max_concurrent BETWEEN 1 AND 200" in MIGRATION
    for col in ("old_values", "new_values", "old_revision", "new_revision", "updated_by", "at "):
        assert col in MIGRATION.split("runner_host_policy_audit (", 1)[1].split(");", 1)[0]


def test_migration_revision_bump_and_audit_triggers():
    assert "NEW.revision := OLD.revision + 1" in MIGRATION
    assert "BEFORE UPDATE ON runner_host_policy" in MIGRATION
    assert "OLD.* IS DISTINCT FROM NEW.*" in MIGRATION
    assert "AFTER INSERT OR UPDATE OR DELETE ON runner_host_policy" in MIGRATION
    assert "to_jsonb(OLD), to_jsonb(NEW), OLD.revision, NEW.revision" in MIGRATION
    # 감사는 이전값·새값·두 revision 을 모두 남긴다
    assert MIGRATION.count("INSERT INTO runner_host_policy_audit") == 3
    assert "IF to_jsonb(OLD) = to_jsonb(NEW) THEN" in MIGRATION  # 값이 같은 UPDATE 는 감사 행을 만들지 않는다


def test_migration_is_additive_and_passes_destructive_gate():
    forward = "\n".join(l.split("--", 1)[0] for l in MIGRATION.splitlines()).upper()
    for banned in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM", "INSERT INTO RUNNER_HOST_POLICY ("):
        assert banned not in forward, banned
    # 운영 슬롯 값을 이 작업에서 넣지 않는다(행 없음 = 기존 동작)
    assert "INSERT INTO RUNNER_HOST_POLICY " not in forward
    down = (ROOT / "migrations/rollback/20261010_runner_host_policy.down.sql").read_text()
    assert "DROP TABLE IF EXISTS runner_host_policy;" in down
