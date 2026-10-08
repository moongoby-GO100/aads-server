"""Actual /proc fixture PIDs and actual Bash mutation gates; no broker/DB calls."""
import os
from pathlib import Path
import subprocess
import tempfile
import types

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts/runner_busy_lib.sh"
SYNC = ROOT / "scripts/sync_pipeline_runner_remote.sh"
LOCAL = ROOT / "scripts/restart_local_runner.sh"


def function(text, name):
    start = text.index(name + "() {")
    return text[start:text.index("\n}\n", start) + 3]


@pytest.fixture
def probe():
    text = LIB.read_text()
    body = text.split("<<'PY_RUNNER_LIVE_PROCESS'\n", 1)[1].split("\nPY_RUNNER_LIVE_PROCESS", 1)[0]
    module = types.ModuleType("actual_runner_process_probe")
    exec(compile(body, str(LIB), "exec"), module.__dict__)
    return module.probe


def properties(state="inactive", main=0, group=""):
    return f"LoadState=loaded\nActiveState={state}\nMainPID={main}\nControlGroup={group}\n"


def roots(tmp_path, pids=(), members=()):
    proc = tmp_path / "proc"
    cg = tmp_path / "cgroup"
    proc.mkdir()
    cg.mkdir()
    for pid in pids:
        (proc / str(pid)).symlink_to(f"/proc/{pid}", target_is_directory=True)
    if members:
        group = cg / "runner.service"
        group.mkdir()
        (group / "cgroup.procs").write_text("\n".join(map(str, members)))
    return proc, cg


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_deleted_cwd_live_worker_outside_cgroup_is_busy(probe, tmp_path, state):
    # Terminal DB/service state cannot make this real, orphan-like PID disappear.
    with tempfile.TemporaryDirectory(prefix="aads-wt-runner-sync-guard-") as cwd:
        child = subprocess.Popen(["sleep", "60"], cwd=cwd)
        try:
            Path(cwd).rmdir()
            assert os.readlink(f"/proc/{child.pid}/cwd").endswith(" (deleted)")
            proc, cg = roots(tmp_path, [child.pid])
            assert probe("runner.service", proc, cg, properties(state)) == "BUSY"
            assert child.poll() is None
        finally:
            child.terminate()
            child.wait(timeout=3)


@pytest.mark.parametrize("state", ["active", "inactive", "failed"])
def test_real_service_child_blocks_even_outside_job_directory(probe, tmp_path, state):
    child = subprocess.Popen(["sleep", "60"], cwd=tmp_path)
    try:
        main = os.getpid() if state == "active" else 0
        pids = [child.pid] + ([main] if main else [])
        proc, cg = roots(tmp_path, pids, pids)
        assert probe("runner.service", proc, cg, properties(state, main, "/runner.service")) == "BUSY"
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=3)


def test_empty_dead_service_has_verified_idle_process_set(probe, tmp_path):
    proc, cg = roots(tmp_path)
    assert probe("runner.service", proc, cg, properties()) == "IDLE"


def test_main_process_alone_is_idle(probe, tmp_path):
    pid = os.getpid()
    proc, cg = roots(tmp_path, [pid], [pid])
    assert probe("runner.service", proc, cg, properties("active", pid, "/runner.service")) == "IDLE"


@pytest.mark.parametrize("failure", ["missing_proc", "missing_cgroup", "invalid_cgroup", "missing_main", "bad_service", "bad_properties"])
def test_incomplete_process_observation_is_unknown(probe, tmp_path, failure):
    proc, cg = roots(tmp_path)
    service, show = "runner.service", properties()
    if failure == "missing_proc":
        proc = tmp_path / "missing"
    elif failure == "missing_cgroup":
        show = properties("inactive", 0, "/missing")
    elif failure == "invalid_cgroup":
        group = cg / "runner.service"
        group.mkdir()
        (group / "cgroup.procs").write_text("invalid-pid")
        show = properties("inactive", 0, "/runner.service")
    elif failure == "missing_main":
        show = properties("active")
    elif failure == "bad_service":
        service = "runner.service'; false #"
    else:
        show = "MainPID=0\n"
    assert probe(service, proc, cg, show) == "UNKNOWN"


def v1_group(cg, layout, members):
    group = cg / layout / "system.slice" / "runner.service"
    group.mkdir(parents=True)
    (group / "cgroup.procs").write_text("\n".join(map(str, members)))
    return group


@pytest.mark.parametrize("layout", ["unified", "systemd"])
def test_cgroup_v1_hybrid_main_process_alone_is_idle(probe, tmp_path, layout):
    pid = os.getpid()
    proc, cg = roots(tmp_path, [pid])
    v1_group(cg, layout, [pid])
    show = properties("active", pid, "/system.slice/runner.service")
    assert probe("runner.service", proc, cg, show) == "IDLE"


@pytest.mark.parametrize("layout", ["unified", "systemd"])
def test_cgroup_v1_hybrid_child_is_busy(probe, tmp_path, layout):
    child = subprocess.Popen(["sleep", "60"], cwd=tmp_path)
    try:
        main = os.getpid()
        proc, cg = roots(tmp_path, [main, child.pid])
        v1_group(cg, layout, [main, child.pid])
        show = properties("active", main, "/system.slice/runner.service")
        assert probe("runner.service", proc, cg, show) == "BUSY"
    finally:
        child.terminate()
        child.wait(timeout=3)


def test_cgroup_v1_hybrid_prefers_root_then_unified_then_systemd(probe, tmp_path):
    child = subprocess.Popen(["sleep", "60"], cwd=tmp_path)
    try:
        main = os.getpid()
        proc, cg = roots(tmp_path, [main])
        v1_group(cg, "unified", [main])
        v1_group(cg, "systemd", [main, child.pid])
        show = properties("active", main, "/system.slice/runner.service")
        assert probe("runner.service", proc, cg, show) == "IDLE"
    finally:
        child.terminate()
        child.wait(timeout=3)


def test_cgroup_v2_root_layout_unchanged_when_v1_dirs_absent(probe, tmp_path):
    pid = os.getpid()
    proc, cg = roots(tmp_path, [pid])
    group = cg / "system.slice" / "runner.service"
    group.mkdir(parents=True)
    (group / "cgroup.procs").write_text(str(pid))
    show = properties("active", pid, "/system.slice/runner.service")
    assert probe("runner.service", proc, cg, show) == "IDLE"


def test_cgroup_missing_in_all_three_layouts_is_unknown(probe, tmp_path):
    pid = os.getpid()
    proc, cg = roots(tmp_path, [pid])
    (cg / "unified").mkdir()
    (cg / "systemd").mkdir()
    show = properties("active", pid, "/system.slice/runner.service")
    assert probe("runner.service", proc, cg, show) == "UNKNOWN"


def test_proc_permission_error_is_unknown(probe, tmp_path, monkeypatch):
    proc, cg = roots(tmp_path)
    original = Path.iterdir
    def denied(path):
        if path == proc:
            raise PermissionError("fixture proc permission failure")
        return original(path)
    monkeypatch.setattr(Path, "iterdir", denied)
    assert probe("runner.service", proc, cg, properties()) == "UNKNOWN"


def test_cgroup_permission_error_is_unknown(probe, tmp_path, monkeypatch):
    proc, cg = roots(tmp_path)
    group = cg / "runner.service"
    group.mkdir()
    original = Path.read_text
    def denied(path, *args, **kwargs):
        if path == group / "cgroup.procs":
            raise PermissionError("fixture cgroup permission failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", denied)
    assert probe("runner.service", proc, cg, properties("inactive", 0, "/runner.service")) == "UNKNOWN"


def test_child_seen_in_cgroup_after_proc_snapshot_still_blocks(probe, tmp_path):
    child = subprocess.Popen(["sleep", "60"], cwd=tmp_path)
    try:
        # The real PID is in cgroup input but was born after the selected proc
        # snapshot. Intersection with old proc rows must not discard it.
        proc, cg = roots(tmp_path, [], [child.pid])
        assert probe("runner.service", proc, cg, properties("inactive", 0, "/runner.service")) == "BUSY"
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=3)


def test_descendant_outside_recorded_cgroup_still_blocks(probe, tmp_path):
    child = subprocess.Popen(["sleep", "60"], cwd=tmp_path)
    try:
        main = os.getpid()
        proc, cg = roots(tmp_path, [main, child.pid], [main])
        assert probe("runner.service", proc, cg, properties("active", main, "/runner.service")) == "BUSY"
    finally:
        child.terminate()
        child.wait(timeout=3)


@pytest.mark.parametrize("observed", ["BUSY", "UNKNOWN", "", "invalid"])
@pytest.mark.parametrize("service_state", ["active", "inactive"])
def test_remote_gate_defers_before_any_mutation_even_with_ignore_busy(tmp_path, observed, service_state):
    # Run the actual sync_one_target function. Stub only SSH/DB boundaries and
    # mutation commands; their execution leaves a local sentinel and fails.
    sentinel = tmp_path / "mutation"
    code = "\n".join([
        'set -euo pipefail',
        f'source "{LIB}"',
        function(SYNC.read_text(), "sync_one_target"),
        'ONLY_TARGET=""; SYNC_REMOTE_UNITS=0; TARGET_STATE_FILE=""; IGNORE_BUSY=1',
        'CANONICAL_RUNNER=/does/not/exist; RESTART_SERVICES=1; DRY_RUN=0',
        'log() { :; }',
        'remote_runner_process_state() { printf "%s" "$PROBE_STATE"; }',
        'remote_runner_host_name() { printf fixture; }',
        'db_active_job_count() { printf 0; }',
        'remote_service_active() { printf "%s" "$SERVICE_STATE"; }',
        'sha256_file() { printf expected; }',
        'remote_sha() { printf current; }',
        'ssh_run() { touch "$SENTINEL"; return 99; }',
        'sync_one_target "fixture|host|/root/scripts/pipeline-runner.sh|runner.service|unused"',
    ])
    result = subprocess.run(["bash", "-c", code], capture_output=True, text=True,
                            env={**os.environ, "PROBE_STATE": observed, "SERVICE_STATE": service_state, "SENTINEL": str(sentinel)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not sentinel.exists()


@pytest.mark.parametrize("observed", ["BUSY", "UNKNOWN"])
def test_local_wrapper_blocks_restart_before_db_and_ignore_override(tmp_path, observed):
    # Execute the actual wrapper, preserving its source call but copying the
    # library with only its OS observation boundary replaced for this fixture.
    directory = tmp_path / "scripts"
    directory.mkdir()
    (directory / LOCAL.name).write_text(LOCAL.read_text())
    (directory / LIB.name).write_text(LIB.read_text() + '\nrunner_live_process_probe() { printf "%s" "$PROBE_STATE"; }\n')
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    sentinel = tmp_path / "called-systemctl"
    command = fakebin / "systemctl"
    command.write_text('#!/bin/sh\ntouch "$SENTINEL"\nexit 99\n')
    command.chmod(0o755)
    result = subprocess.run(["bash", str(directory / LOCAL.name), "--ignore-busy"], capture_output=True, text=True,
                            env={**os.environ, "PATH": str(fakebin) + ":" + os.environ["PATH"], "PROBE_STATE": observed, "SENTINEL": str(sentinel)})
    assert result.returncode == 3, result.stdout + result.stderr
    assert not sentinel.exists()


def test_remote_probe_sends_executable_function_without_install(tmp_path):
    # Exercise the actual SSH payload: the transport executes it locally and
    # systemctl supplies a loaded, empty service; /proc is real and read-only.
    code = '\n'.join([
        f'source "{LIB}"',
        function(SYNC.read_text(), "remote_runner_process_state"),
        'SSH_OPTS=()',
        'ssh() { shift; bash -c "$1"; }',
        'remote_runner_process_state fixture "missing-synthetic.service"',
    ])
    result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout == "UNKNOWN"  # systemctl cannot find a loaded fixture service.
