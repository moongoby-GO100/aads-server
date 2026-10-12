"""Fake /proc + cgroup trees: idle polling children are not BUSY (AADS-RUNNER-BUSY-PROBE-IDLE-POLL)."""
import os
from pathlib import Path
import subprocess
import types

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts/runner_busy_lib.sh"
RUNNER = "/root/scripts/pipeline-runner.sh"
MAIN = 100
UPTIME = 100000
TICKS = os.sysconf("SC_CLK_TCK")


@pytest.fixture
def probe():
    text = LIB.read_text()
    body = text.split("<<'PY_RUNNER_LIVE_PROCESS'\n", 1)[1].split("\nPY_RUNNER_LIVE_PROCESS", 1)[0]
    module = types.ModuleType("actual_runner_process_probe")
    exec(compile(body, str(LIB), "exec"), module.__dict__)
    return module.probe


class Tree:
    def __init__(self, tmp_path):
        self.proc, self.cg = tmp_path / "proc", tmp_path / "cgroup"
        self.proc.mkdir()
        (self.proc / "uptime").write_text(f"{UPTIME}.00 0.00\n")
        (self.cg / "runner.service").mkdir(parents=True)
        self.members = []
        self.add(MAIN, 1, ["bash", RUNNER], age=300000)

    def add(self, pid, ppid, argv, age=5, cwd="/root", member=True):
        d = self.proc / str(pid)
        d.mkdir()
        start = int((UPTIME - age) * TICKS)
        # fields after ')': state ppid pgrp session tty tpgid flags ... starttime is index 19
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)]
        (d / "stat").write_text(f"{pid} (x) " + " ".join(fields) + " 0\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "cwd").symlink_to(cwd)
        if member:
            self.members.append(pid)
            (self.cg / "runner.service" / "cgroup.procs").write_text("\n".join(map(str, self.members)))

    def show(self):
        return f"LoadState=loaded\nActiveState=active\nMainPID={MAIN}\nControlGroup=/runner.service\n"

    def run(self, probe, **kwargs):
        reasons = []
        state = probe("runner.service", self.proc, self.cg, self.show(), reasons=reasons, **kwargs)
        return state, reasons


@pytest.fixture
def tree(tmp_path):
    return Tree(tmp_path)


def test_1_main_plus_sleep_5_is_idle(probe, tree):
    tree.add(200, MAIN, ["sleep", "5"])
    assert tree.run(probe) == ("IDLE", [])


def test_2_idle_poll_subshells_sleep_psql_are_idle(probe, tree):
    for pid in (201, 202, 203):
        tree.add(pid, MAIN, ["bash", RUNNER])
    tree.add(204, 201, ["sleep", "30"])
    tree.add(205, 202, ["psql", "-h", "127.0.0.1", "-p", "15433", "-U", "aads"])
    tree.add(206, 203, ["timeout", "30", "psql", "-h", "127.0.0.1"])
    tree.add(207, MAIN, ["timeout", "-k", "5", "30", "/usr/bin/psql", "-At"])
    assert tree.run(probe) == ("IDLE", [])


def test_3_worktree_cwd_is_busy(probe, tree):
    tree.add(200, MAIN, ["git", "status"], cwd="/tmp/aads-wt-runner-abc")
    state, reasons = tree.run(probe)
    assert state == "BUSY" and "pid=200" in reasons[0] and "/tmp/aads-wt-runner-abc" in reasons[0]


@pytest.mark.parametrize("argv", [
    ["/root/aads/vendor/claude-cli/claude", "--model", "x", "-p", "do it"],
    ["timeout", "3600", "/root/aads/vendor/claude-cli/claude", "-p", "do it"],
    ["env", "-u", "RUNNER_TEST_UNSET_VAR", "/root/scripts/claude-slot-credentials-wrapper.sh", "timeout", "9", "claude"],
    ["timeout", "3600", "codex", "exec", "task"],
    ["/root/aads-litellm-runner-venv/bin/python", "/root/scripts/litellm_runner.py", "--model", "m"],
])
def test_4_worker_cli_child_is_busy(probe, tree, argv):
    tree.add(200, MAIN, ["bash", RUNNER])
    tree.add(201, 200, argv)
    state, reasons = tree.run(probe)
    assert state == "BUSY" and "pid=201 worker" in reasons[0]


def test_5_stale_orphan_loop_is_busy_and_names_pid(probe, tree):
    tree.add(200, MAIN, ["bash", RUNNER], age=25 * 3600)
    tree.add(201, 200, ["sleep", "30"])
    state, reasons = tree.run(probe)
    assert state == "BUSY"
    assert "pid=200 stale runner subshell" in reasons[0] and "age=90000s" in reasons[0]


def test_5b_old_non_wait_child_is_busy(probe, tree):
    tree.add(200, MAIN, ["rsync", "-a", "x", "y"], age=1200)
    state, reasons = tree.run(probe)
    assert state == "BUSY" and "pid=200 long-running child" in reasons[0]


def test_5c_fresh_non_wait_child_is_idle(probe, tree):
    tree.add(200, MAIN, ["curl", "-s", "http://x"], age=3)
    assert tree.run(probe) == ("IDLE", [])


def test_5d_old_sleep_and_psql_still_ignored(probe, tree):
    tree.add(200, MAIN, ["sleep", "9999"], age=7200)
    tree.add(201, MAIN, ["psql", "-c", "select pg_sleep(1)"], age=7200)
    assert tree.run(probe) == ("IDLE", [])


def test_5e_cgroup_member_missing_from_proc_is_busy(probe, tree):
    tree.members.append(999)
    (tree.cg / "runner.service" / "cgroup.procs").write_text("\n".join(map(str, tree.members)))
    state, reasons = tree.run(probe)
    assert state == "BUSY" and "pid=999 unobservable" in reasons[0]


def test_6_cgroup_read_failure_is_unknown(probe, tree):
    tree.add(200, MAIN, ["sleep", "5"])
    (tree.cg / "runner.service" / "cgroup.procs").write_text("not-a-pid")
    assert tree.run(probe) == ("UNKNOWN", [])


def test_6b_cgroup_dir_missing_is_unknown(probe, tree):
    (tree.cg / "runner.service" / "cgroup.procs").unlink()
    (tree.cg / "runner.service").rmdir()
    assert tree.run(probe) == ("UNKNOWN", [])


def test_main_entry_invalid_service_is_unknown_without_reason():
    body = LIB.read_text().split("<<'PY_RUNNER_LIVE_PROCESS'\n", 1)[1].split("\nPY_RUNNER_LIVE_PROCESS", 1)[0]
    result = subprocess.run(["python3", "-", "bad name!"], input=body, capture_output=True, text=True)
    assert result.stdout == "UNKNOWN\n"
