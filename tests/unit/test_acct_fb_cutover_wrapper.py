"""Behavioural tests for scripts/deploy_acct_fb_cutover.sh (ops target ACCT/fb-cutover).

The real cutover script is replaced by a recording fake and `curl` by a stub, so these
runs never touch cafe24, nginx or any firewall.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
WRAPPER = ROOT / "scripts/deploy_acct_fb_cutover.sh"
CAFE24_IP = "114.207.244.86"

FAKE_CUTOVER = r"""#!/bin/bash
echo "$1 fw_confirm=${CONFIRM_FIREWALL_CHANGE:-} monitor_seconds=${MONITOR_SECONDS:-}" >> "$FAKE_DIR/calls.log"
[[ ${FAKE_FAIL_STEP:-} == "$1" ]] && exit 1
case "$1" in
  allow-edge-fw) touch "$FAKE_DIR/fw_open" ;;
  revoke-edge-fw) rm -f "$FAKE_DIR/fw_open" ;;
  apply-edge)
    [[ -n ${FAKE_EDGE_LINE:-} ]] && printf '%s\n' "$FAKE_EDGE_LINE" > "$EDGE_CONF" ;;
  monitor)
    [[ -n ${FAKE_MONITOR_HANG:-} ]] && exec sleep 60 ;;
esac
exit 0
"""

FAKE_CURL = """#!/bin/bash
if [[ -e "$FAKE_DIR/fw_open" || -n "${FAKE_REACH_OK:-}" ]]; then printf 200; else printf 000; fi
"""


@pytest.fixture
def sandbox(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "config/apache").mkdir(parents=True)
    (repo / "scripts/cutover_fb_cafe24.sh").write_text(FAKE_CUTOVER)
    (repo / "config/apache/fb-cafe24.conf").write_text("template\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(FAKE_CURL)
    (bindir / "curl").chmod(0o755)
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    edge = tmp_path / "fb.conf"
    edge.write_text("proxy_pass http://yeoljeong_finance_api;\n")
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AADS_DEPLOY_REPO_DIR": str(repo),
        "ACCT_FB_LOCK_FILE": str(tmp_path / "lock"),
        "FW_APPROVAL_FILE": str(tmp_path / "fw.approved"),
        "EDGE_CONF": str(edge),
        "FAKE_DIR": str(fake_dir),
        "MONITOR_SECONDS": "7",
    }
    env.pop("CONFIRM_FIREWALL_CHANGE", None)
    return SimpleSandbox(repo, sha, env, fake_dir, edge, tmp_path)


class SimpleSandbox:
    def __init__(self, repo, sha, env, fake_dir, edge, tmp_path):
        self.repo, self.sha, self.env, self.fake_dir, self.edge, self.tmp = repo, sha, env, fake_dir, edge, tmp_path

    def run(self, sha=None, **extra):
        env = {**self.env, **extra}
        return subprocess.run(
            ["bash", str(WRAPPER), sha or self.sha, "42"], env=env, capture_output=True, text=True, timeout=60
        )

    def calls(self):
        log = self.fake_dir / "calls.log"
        return [line.split()[0] for line in log.read_text().splitlines()] if log.exists() else []

    def call_lines(self):
        log = self.fake_dir / "calls.log"
        return log.read_text().splitlines() if log.exists() else []


def test_script_has_valid_bash_syntax():
    subprocess.run(["bash", "-n", str(WRAPPER)], check=True)


def test_happy_path_runs_steps_in_order_without_touching_firewall(sandbox):
    proc = sandbox.run(FAKE_REACH_OK="1", FAKE_EDGE_LINE=f"proxy_pass https://{CAFE24_IP};")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sandbox.calls() == ["lint", "preflight", "apply-origin", "apply-edge", "monitor"]
    assert "7s monitor clean" in proc.stdout and "300s" not in proc.stdout
    assert "monitor_seconds=7" in sandbox.call_lines()[-1]


@pytest.mark.parametrize("stage", ["lint", "preflight", "apply-origin"])
def test_pre_edge_failure_exits_9_and_never_touches_edge(sandbox, stage):
    proc = sandbox.run(FAKE_REACH_OK="1", FAKE_FAIL_STEP=stage)
    assert proc.returncode == 9
    assert "apply-edge" not in sandbox.calls() and "maintenance" not in sandbox.calls()
    assert "revoke-edge-fw" not in sandbox.calls()


def test_unreachable_origin_without_approval_exits_8_and_does_not_open_firewall(sandbox):
    proc = sandbox.run()
    assert proc.returncode == 8, proc.stdout + proc.stderr
    assert sandbox.calls() == ["lint", "preflight", "apply-origin"]
    assert not (sandbox.fake_dir / "fw_open").exists()


def test_approval_file_for_another_sha_is_not_approval(sandbox):
    (sandbox.tmp / "fw.approved").write_text("0" * 40 + "\n")
    assert sandbox.run().returncode == 8
    assert "allow-edge-fw" not in sandbox.calls()


def test_approval_file_for_this_sha_opens_firewall_then_continues(sandbox):
    (sandbox.tmp / "fw.approved").write_text(sandbox.sha + "\n")
    proc = sandbox.run(FAKE_EDGE_LINE=f"proxy_pass https://{CAFE24_IP};")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sandbox.calls() == ["lint", "preflight", "apply-origin", "allow-edge-fw", "apply-edge", "monitor"]
    assert "fw_confirm=1" in [line for line in sandbox.call_lines() if line.startswith("allow-edge-fw")][0]
    assert (sandbox.fake_dir / "fw_open").exists()


def test_allow_edge_fw_failure_exits_10_and_revokes_the_rule(sandbox):
    (sandbox.tmp / "fw.approved").write_text(sandbox.sha + "\n")
    proc = sandbox.run(FAKE_FAIL_STEP="allow-edge-fw")
    assert proc.returncode == 10
    assert sandbox.calls()[-1] == "revoke-edge-fw"
    assert "apply-edge" not in sandbox.calls()


def test_apply_edge_failure_exits_6_with_maintenance_and_firewall_revoked(sandbox):
    (sandbox.tmp / "fw.approved").write_text(sandbox.sha + "\n")
    proc = sandbox.run(FAKE_FAIL_STEP="apply-edge")
    assert proc.returncode == 6
    assert sandbox.calls()[-1] == "revoke-edge-fw"
    assert "maintenance" not in sandbox.calls()  # edge never pointed at cafe24
    assert not (sandbox.fake_dir / "fw_open").exists()


def test_monitor_failure_exits_7_and_writes_maintenance(sandbox):
    proc = sandbox.run(FAKE_REACH_OK="1", FAKE_EDGE_LINE=f"proxy_pass https://{CAFE24_IP};", FAKE_FAIL_STEP="monitor")
    assert proc.returncode == 7
    assert sandbox.calls()[-1] == "maintenance"
    assert "revoke-edge-fw" not in sandbox.calls()


@pytest.mark.parametrize(
    "edge_line, expect_maintenance",
    [
        (f"proxy_pass   https://{CAFE24_IP}:443;", True),
        (f"proxy_pass https://{CAFE24_IP}/;", True),
        (f"# proxy_pass https://{CAFE24_IP};", False),
        ("proxy_pass https://114x207x244x86;", False),
        (f"proxy_pass https://{CAFE24_IP}9;", False),
    ],
)
def test_edge_detection_tolerates_format_but_not_lookalikes(sandbox, edge_line, expect_maintenance):
    proc = sandbox.run(FAKE_REACH_OK="1", FAKE_EDGE_LINE=edge_line, FAKE_FAIL_STEP="monitor")
    assert proc.returncode == 7
    assert ("maintenance" in sandbox.calls()) is expect_maintenance


def test_term_during_monitor_stops_promptly_and_recovers(sandbox):
    env = {**sandbox.env, "FAKE_REACH_OK": "1", "FAKE_EDGE_LINE": f"proxy_pass https://{CAFE24_IP};", "FAKE_MONITOR_HANG": "1"}
    proc = subprocess.Popen(
        ["bash", str(WRAPPER), sandbox.sha, "42"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    deadline = time.time() + 15
    while "monitor" not in sandbox.calls() and time.time() < deadline:
        time.sleep(0.1)
    assert "monitor" in sandbox.calls()
    started = time.time()
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 143, out
    assert time.time() - started < 15
    assert sandbox.calls()[-1] == "maintenance"


def test_exhausted_budget_refuses_to_start_any_step(sandbox):
    proc = sandbox.run(FAKE_REACH_OK="1", DEPLOY_BUDGET_SECONDS="0")
    assert proc.returncode == 9
    assert "time budget exhausted" in proc.stdout
    assert sandbox.calls() == []


@pytest.mark.parametrize("bad", ["", "zzzz", "abc", "g" * 40])
def test_invalid_sha_exits_2(sandbox, bad):
    assert sandbox.run(sha=bad or "x").returncode == 2


def test_head_mismatch_exits_4(sandbox):
    git = ["git", "-C", str(sandbox.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "second"], check=True)
    proc = sandbox.run(sha=sandbox.sha)
    assert proc.returncode == 4
    assert sandbox.calls() == []


def test_dirty_cutover_script_exits_5_but_untracked_noise_does_not(sandbox):
    (sandbox.repo / "config/apache/untracked.tmp").write_text("x")
    proc = sandbox.run(FAKE_REACH_OK="1", FAKE_EDGE_LINE=f"proxy_pass https://{CAFE24_IP};")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (sandbox.fake_dir / "calls.log").unlink()
    with (sandbox.repo / "scripts/cutover_fb_cafe24.sh").open("a") as fh:
        fh.write("# edit\n")
    assert sandbox.run(FAKE_REACH_OK="1").returncode == 5
    assert sandbox.calls() == []


def test_untracked_cutover_script_exits_5(sandbox):
    subprocess.run(["git", "-C", str(sandbox.repo), "rm", "-q", "--cached", "scripts/cutover_fb_cafe24.sh"], check=True)
    subprocess.run(
        ["git", "-C", str(sandbox.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "untrack"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(sandbox.repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert sandbox.run(sha=sha).returncode == 5


def test_second_run_is_refused_while_lock_is_held(sandbox):
    holder = subprocess.Popen(["flock", "-n", str(sandbox.tmp / "lock"), "sleep", "30"])
    try:
        time.sleep(0.5)
        assert sandbox.run(FAKE_REACH_OK="1").returncode == 3
    finally:
        holder.kill()
        holder.wait()
