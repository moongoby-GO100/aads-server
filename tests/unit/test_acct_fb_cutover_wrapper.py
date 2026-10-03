"""Behavioural tests for scripts/deploy_acct_fb_cutover.sh (ops target ACCT/fb-cutover).

The real cutover script is replaced by a recording fake and `curl` by a stub, so these
runs never touch cafe24, nginx or any firewall. The tests of the real cutover script put fake
`ssh`/`iptables`/`curl`/`docker`/`sleep` first on PATH; each fake records whether the shared nginx
switch lock is held at call time, which is how the "short lock" contract is checked.
"""

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[2]
WRAPPER = ROOT / "scripts/deploy_acct_fb_cutover.sh"
CAFE24_IP = "114.207.244.86"

FAKE_CUTOVER = r"""#!/bin/bash
echo "$1 fw_confirm=${CONFIRM_FIREWALL_CHANGE:-} monitor_seconds=${MONITOR_SECONDS:-} script=$0" >> "$FAKE_DIR/calls.log"
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


# ---------------------------------------------------------------------------------------------------
# Release worktree isolation (wrapper without AADS_DEPLOY_REPO_DIR)
# ---------------------------------------------------------------------------------------------------

def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def wt(sandbox, tmp_path):
    """Host-like source checkout: release pushed to origin, host HEAD ahead by one commit and dirty."""
    src = tmp_path / "hostsrc"
    shutil.copytree(sandbox.repo, src)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(src, "remote", "add", "origin", str(bare))
    _git(src, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(src, "fetch", "-q", "origin")
    release = _git(src, "rev-parse", "HEAD")
    (src / "scripts/cutover_fb_cafe24.sh").write_text("#!/bin/bash\necho HOSTCOPY >> \"$FAKE_DIR/host_used\"\nexit 0\n")
    _git(src, "commit", "-q", "-am", "host-only unpushed")
    host_head = _git(src, "rev-parse", "HEAD")
    (src / "config/apache/fb-cafe24.conf").write_text("dirty host template\n")
    root = tmp_path / "releases"
    env = {k: v for k, v in sandbox.env.items() if k != "AADS_DEPLOY_REPO_DIR"}
    env.update(AADS_DEPLOY_SOURCE_REPO=str(src), ACCT_FB_RELEASE_ROOT=str(root))
    return SimpleNamespace(sandbox=sandbox, src=src, release=release, host_head=host_head, root=root, env=env)


def _run_wt(wt, sha=None, **extra):
    return subprocess.run(
        ["bash", str(WRAPPER), sha or wt.release, "42"], env={**wt.env, **extra},
        capture_output=True, text=True, timeout=60,
    )


def _leftover(wt):
    return [p.name for p in wt.root.iterdir()] if wt.root.exists() else []


def test_without_repo_dir_runs_exactly_the_release_sha_in_an_isolated_worktree(wt):
    sb = wt.sandbox
    proc = _run_wt(wt, FAKE_REACH_OK="1", FAKE_EDGE_LINE=f"proxy_pass https://{CAFE24_IP};")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sb.calls() == ["lint", "preflight", "apply-origin", "apply-edge", "monitor"]
    scripts = {line.split("script=")[1] for line in sb.call_lines()}
    assert len(scripts) == 1 and str(wt.root) in scripts.pop()
    assert not (sb.fake_dir / "host_used").exists()
    assert _git(wt.src, "rev-parse", "HEAD") == wt.host_head != wt.release
    assert (wt.src / "config/apache/fb-cafe24.conf").read_text() == "dirty host template\n"
    assert _leftover(wt) == []
    assert str(wt.root) not in _git(wt.src, "worktree", "list")


def test_isolated_worktree_is_removed_after_a_failed_run(wt):
    assert _run_wt(wt, FAKE_REACH_OK="1", FAKE_FAIL_STEP="preflight").returncode == 9
    assert _leftover(wt) == []
    assert str(wt.root) not in _git(wt.src, "worktree", "list")


def test_unpushed_release_sha_fails_closed(wt):
    assert _run_wt(wt, sha=wt.host_head, FAKE_REACH_OK="1").returncode == 5
    assert wt.sandbox.calls() == [] and _leftover(wt) == []


def test_unknown_release_sha_fails_closed(wt):
    assert _run_wt(wt, sha="1" * 40, FAKE_REACH_OK="1").returncode == 4
    assert wt.sandbox.calls() == [] and _leftover(wt) == []


def test_abbreviated_sha_is_refused_when_the_worktree_is_built_by_the_wrapper(wt):
    proc = _run_wt(wt, sha=wt.release[:12], FAKE_REACH_OK="1")
    assert proc.returncode == 2 and "40-hex" in proc.stdout
    assert wt.sandbox.calls() == []


def test_missing_source_repo_fails_closed(wt):
    assert _run_wt(wt, AADS_DEPLOY_SOURCE_REPO=str(wt.sandbox.tmp / "nope")).returncode == 4
    assert wt.sandbox.calls() == []


def test_firewall_approval_for_the_host_head_does_not_approve_the_release(wt):
    (wt.sandbox.tmp / "fw.approved").write_text(wt.host_head + "\n")
    assert _run_wt(wt).returncode == 8
    assert "allow-edge-fw" not in wt.sandbox.calls()


def test_firewall_approval_for_the_full_release_sha_is_honoured_in_the_isolated_worktree(wt):
    (wt.sandbox.tmp / "fw.approved").write_text(wt.release + "\n")
    proc = _run_wt(wt, FAKE_EDGE_LINE=f"proxy_pass https://{CAFE24_IP};")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert wt.sandbox.calls() == ["lint", "preflight", "apply-origin", "allow-edge-fw", "apply-edge", "monitor"]


def test_repo_dir_override_is_still_used_as_is(wt):
    explicit = {**wt.env, "AADS_DEPLOY_REPO_DIR": str(wt.sandbox.repo)}
    proc = subprocess.run(["bash", str(WRAPPER), wt.sandbox.sha, "42"], env={**explicit, "FAKE_REACH_OK": "1"},
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not wt.root.exists() or _leftover(wt) == []
    assert str(wt.sandbox.repo) in wt.sandbox.call_lines()[0]


# ---------------------------------------------------------------------------------------------------
# Real cutover_fb_cafe24.sh against fake ssh/iptables/curl/docker/sleep
# ---------------------------------------------------------------------------------------------------

CUTOVER = ROOT / "scripts/cutover_fb_cafe24.sh"
EDGE_IP = "5.104.86.116"
EXACT_RULE = f"-s {EDGE_IP}/32 -p tcp -m tcp --dport 443 -m comment --comment fb-edge-contabo116 -j ACCEPT"
LEGACY_RULE = f"-s {EDGE_IP}/32 -m comment --comment fb-edge-contabo116 -j ACCEPT"
OTHER_RULE = f"-s {EDGE_IP}/32 -m comment --comment someone-else -j ACCEPT"
EDGE_FIXTURE = """server {
    listen 443 ssl;
    server_name fb.newtalk.kr;
    location /api/ { proxy_pass http://yeoljeong_finance_api; }
    location /dash/ { proxy_pass http://aads_dashboard; }
}
"""

_LOCKSTATE = 'lockstate() { flock -n "$NGINX_SWITCH_LOCK" true && echo free || echo held; }\n'

FAKES = {
    "ssh": _LOCKSTATE + r"""cmd="${@: -1}"
echo "ssh ${cmd:0:60} lock=$(lockstate)" >> "$FAKE_DIR/calls.log"
[[ -n ${FAKE_SSH_FAIL:-} && $cmd == *"$FAKE_SSH_FAIL"* ]] && exit 255
case "$cmd" in
  grep\ -c*) printf '%s\n' "${FAKE_LOG_SEEN:-1}" ;;
  *"bash -s"*) bash -c "$cmd"; exit $? ;;
esac
exit 0
""",
    "sleep": _LOCKSTATE + 'echo "sleep $* lock=$(lockstate)" >> "$FAKE_DIR/calls.log"\n',
    "docker": _LOCKSTATE + r"""echo "docker $* lock=$(lockstate)" >> "$FAKE_DIR/calls.log"
[[ -n ${FAKE_NGINX_T_FAIL:-} && " $* " == *" -t "* ]] && exit 1
exit 0
""",
    "curl": _LOCKSTATE + r"""args="$*"; max=""; prev=""; insecure=0
for a in "$@"; do [[ $prev == -m ]] && max=$a; [[ $a == --insecure || $a =~ ^-[a-zA-Z]*k[a-zA-Z]*$ ]] && insecure=1; prev=$a; done
if [[ $args == *":127.0.0.1"* ]]; then kind=routed; else kind=direct; fi
url="${@: -1}"
echo "curl kind=$kind max=$max insecure=$insecure url=$url lock=$(lockstate)" >> "$FAKE_DIR/calls.log"
code=200
if [[ $kind == direct ]]; then
  [[ -n ${FAKE_DIRECT_CODE:-} ]] && code=$FAKE_DIRECT_CODE
  [[ -n ${FAKE_FW_GATED:-} && ! -s "$FAKE_DIR/ipt.rules" ]] && code=000
else
  [[ $url == *probe=* && -n ${FAKE_ROUTED_FAIL:-} ]] && code=503
  [[ $url == */static/* && -n ${FAKE_QA_FAIL:-} ]] && code=500
  [[ $url == */api/v1/health ]] && code=401
fi
printf '%s' "$code"
""",
    "iptables": r"""f="$FAKE_DIR/ipt.rules"; touch "$f"
op="$1"; chain="$2"; shift 2
echo "iptables $op $chain $*" >> "$FAKE_DIR/ipt.log"
case "$op" in
  -C) grep -Fxq -- "$*" "$f" ;;
  -I) shift; { printf '%s\n' "$*"; cat "$f"; } > "$f.n"; mv "$f.n" "$f" ;;
  -D) grep -Fxq -- "$*" "$f" || exit 1
      awk -v r="$*" '!d && $0 == r { d = 1; next } { print }' "$f" > "$f.n"; mv "$f.n" "$f" ;;
  -S) echo "-N $chain"; while IFS= read -r l; do echo "-A $chain $l"; done < "$f" ;;
  *) exit 2 ;;
esac
""",
}


@pytest.fixture
def cut(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name, body in FAKES.items():
        (bindir / name).write_text("#!/bin/bash\n" + body)
        (bindir / name).chmod(0o755)
    fake_dir = tmp_path / "cutfake"
    fake_dir.mkdir()
    edge = tmp_path / "edge-fb.conf"
    edge.write_text(EDGE_FIXTURE)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_DIR": str(fake_dir),
        "EDGE_CONF": str(edge),
        "NGINX_SWITCH_LOCK": str(tmp_path / "nginx.lock"),
        "FB_STATE_MARKER": str(tmp_path / "fb.state"),
    }
    for k in ("CONFIRM_FIREWALL_CHANGE", "CONFIRM_REMOVE_BROAD_FW"):
        env.pop(k, None)
    return SimpleNamespace(env=env, fake=fake_dir, edge=edge, tmp=tmp_path, marker=tmp_path / "fb.state")


def _cut(c, *args, **extra):
    return subprocess.run(["bash", str(CUTOVER), *args], env={**c.env, **extra}, capture_output=True, text=True, timeout=60)


def _rules(c):
    f = c.fake / "ipt.rules"
    return f.read_text().splitlines() if f.exists() else []


def _seed(c, *rules):
    (c.fake / "ipt.rules").write_text("".join(r + "\n" for r in rules))


def _log(c):
    f = c.fake / "calls.log"
    return f.read_text().splitlines() if f.exists() else []


def _kind(lines, prefix):
    return [line for line in lines if line.startswith(prefix)]


def test_cutover_script_syntax_and_lint_pass():
    subprocess.run(["bash", "-n", str(CUTOVER)], check=True)


def test_allow_edge_fw_adds_only_tcp_443_and_is_idempotent(cut):
    for _ in range(2):
        proc = _cut(cut, "allow-edge-fw", CONFIRM_FIREWALL_CHANGE="1")
        assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _rules(cut) == [EXACT_RULE]
    assert "--dport 80" not in EXACT_RULE and "--dport 443" in _rules(cut)[0]
    assert len(_kind((cut.fake / "ipt.log").read_text().splitlines(), "iptables -I")) == 1
    assert "already present" in proc.stdout


def test_firewall_change_requires_explicit_confirmation(cut):
    proc = _cut(cut, "allow-edge-fw")
    assert proc.returncode != 0 and "CONFIRM_FIREWALL_CHANGE" in proc.stderr
    assert _rules(cut) == [] and _log(cut) == []


def test_revoke_edge_fw_removes_exactly_the_443_rule_and_is_idempotent(cut):
    _seed(cut, OTHER_RULE, EXACT_RULE)
    for _ in range(2):
        assert _cut(cut, "revoke-edge-fw", CONFIRM_FIREWALL_CHANGE="1").returncode == 0
    assert _rules(cut) == [OTHER_RULE]


def test_broad_legacy_rule_is_not_adopted_widened_or_silently_removed(cut):
    _seed(cut, LEGACY_RULE)
    proc = _cut(cut, "allow-edge-fw", CONFIRM_FIREWALL_CHANGE="1")
    assert proc.returncode != 0 and "broad" in proc.stdout
    assert _rules(cut) == [LEGACY_RULE]
    proc = _cut(cut, "revoke-edge-fw", CONFIRM_FIREWALL_CHANGE="1")
    assert proc.returncode == 0 and "WARN broad" in proc.stdout
    assert _rules(cut) == [LEGACY_RULE]
    assert _cut(cut, "edge-fw-status").returncode == 3


def test_broad_legacy_rule_removed_only_with_second_confirmation_and_only_if_ours(cut):
    _seed(cut, OTHER_RULE, LEGACY_RULE)
    proc = _cut(cut, "revoke-edge-fw", CONFIRM_FIREWALL_CHANGE="1", CONFIRM_REMOVE_BROAD_FW="1")
    assert proc.returncode == 0, proc.stdout
    assert _rules(cut) == [OTHER_RULE]
    assert "SKIP not our comment" in proc.stdout
    assert _cut(cut, "edge-fw-status").returncode == 3  # foreign broad rule is reported, never deleted
    _seed(cut)
    assert _cut(cut, "allow-edge-fw", CONFIRM_FIREWALL_CHANGE="1").returncode == 0
    assert _rules(cut) == [EXACT_RULE]
    assert _cut(cut, "edge-fw-status").returncode == 0


def _apply_edge(c, **extra):
    return _cut(c, "apply-edge", **extra)


def test_apply_edge_holds_the_nginx_lock_only_for_config_reload_and_local_routed_health(cut):
    proc = _apply_edge(cut)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = _log(cut)
    assert all(line.endswith("lock=free") for line in _kind(log, "ssh ") + _kind(log, "sleep ")), log
    direct = _kind(log, "curl kind=direct")
    assert direct and all(line.endswith("lock=free") for line in direct)
    docker = _kind(log, "docker ")
    assert len(docker) == 2 and all(line.endswith("lock=held") for line in docker)  # nginx -t, reload
    routed_probe = [line for line in _kind(log, "curl kind=routed") if "probe=" in line]
    assert len(routed_probe) == 1 and routed_probe[0].endswith("lock=held")
    assert re.search(r"max=[1-5] ", routed_probe[0])
    qa = [line for line in _kind(log, "curl kind=routed") if "probe=" not in line]
    assert qa and all(line.endswith("lock=free") for line in qa)
    last_docker = max(i for i, line in enumerate(log) if line.startswith("docker "))
    proof = [i for i, line in enumerate(log) if line.startswith("ssh grep")]
    assert proof and min(proof) > last_docker
    assert proc.stdout.count("nginx switch lock held") == 1
    assert "state=cafe24-verified" in cut.marker.read_text()
    assert f"proxy_pass https://{CAFE24_IP}" in cut.edge.read_text()
    assert "http://aads_dashboard" in cut.edge.read_text()


def test_routed_health_failure_inside_the_lock_rolls_back_to_maintenance_in_the_same_lock(cut):
    proc = _apply_edge(cut, FAKE_ROUTED_FAIL="1")
    assert proc.returncode != 0
    log = _log(cut)
    assert all(line.endswith("lock=free") for line in _kind(log, "ssh ") + _kind(log, "sleep "))
    assert not _kind(log, "ssh grep")
    assert all(line.endswith("lock=held") for line in _kind(log, "docker "))
    assert len(_kind(log, "docker ")) == 4  # cutover test+reload, maintenance test+reload
    assert proc.stdout.count("nginx switch lock held") == 1
    body = cut.edge.read_text()
    assert "503" in body and "yeoljeong_finance_api" not in body and "jinah" not in body.lower()
    assert "state=maintenance" in cut.marker.read_text()


@pytest.mark.parametrize("fail", ["log", "qa"])
def test_post_unlock_verification_failure_relocks_only_to_roll_back(cut, fail):
    extra = {"FAKE_LOG_SEEN": "0"} if fail == "log" else {"FAKE_QA_FAIL": "1"}
    proc = _apply_edge(cut, **extra)
    assert proc.returncode != 0 and "rolled back to maintenance" in proc.stdout + proc.stderr
    log = _log(cut)
    assert all(line.endswith("lock=free") for line in _kind(log, "ssh ") + _kind(log, "sleep "))
    assert all(line.endswith("lock=held") for line in _kind(log, "docker "))
    assert len(_kind(log, "docker ")) == 4
    assert proc.stdout.count("nginx switch lock held") == 2  # cutover window, then rollback window
    body = cut.edge.read_text()
    assert "503" in body and "yeoljeong_finance_api" not in body
    assert "state=maintenance" in cut.marker.read_text()
    first_proof = min(i for i, line in enumerate(log) if line.startswith("ssh grep") or "kind=routed" in line and "probe=" not in line)
    assert first_proof > [i for i, line in enumerate(log) if line.startswith("docker ")][1]


def test_nginx_test_failure_restores_the_previous_file_and_exits_nonzero(cut):
    proc = _apply_edge(cut, FAKE_NGINX_T_FAIL="1")
    assert proc.returncode != 0
    assert cut.edge.read_text() == EDGE_FIXTURE
    assert not _kind(_log(cut), "ssh grep")


@pytest.mark.parametrize(
    "extra, seed",
    [
        ({"FAKE_DIRECT_CODE": "000"}, []),
        ({}, [LEGACY_RULE]),
        ({"FAKE_SSH_FAIL": "test -r"}, []),
        ({"FAKE_SSH_FAIL": "bash -s"}, []),
    ],
)
def test_preparation_failures_never_take_the_nginx_lock_or_touch_nginx(cut, extra, seed):
    _seed(cut, *seed)
    proc = _apply_edge(cut, **extra)
    assert proc.returncode != 0
    assert not _kind(_log(cut), "docker ") and "nginx switch lock held" not in proc.stdout
    assert cut.edge.read_text() == EDGE_FIXTURE and not cut.marker.exists()


def test_maintenance_command_uses_the_same_short_lock(cut):
    proc = _cut(cut, "maintenance")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert all(line.endswith("lock=held") for line in _kind(_log(cut), "docker "))
    assert not _kind(_log(cut), "ssh ") and "state=maintenance" in cut.marker.read_text()


def test_wrapper_failure_exit_revokes_the_443_rule_and_leaves_maintenance(cut, sandbox):
    shim = (
        '#!/bin/bash\ncase "$1" in lint|preflight|apply-origin) exit 0;; esac\n'
        'exec bash "$REAL_CUTOVER" "$@"\n'
    )
    (sandbox.repo / "scripts/cutover_fb_cafe24.sh").write_text(shim)
    _git(sandbox.repo, "commit", "-q", "-am", "shim")
    sha = _git(sandbox.repo, "rev-parse", "HEAD")
    (sandbox.tmp / "fw.approved").write_text(sha + "\n")
    env = {
        **cut.env,
        "AADS_DEPLOY_REPO_DIR": str(sandbox.repo),
        "ACCT_FB_LOCK_FILE": str(sandbox.tmp / "wlock"),
        "FW_APPROVAL_FILE": str(sandbox.tmp / "fw.approved"),
        "REAL_CUTOVER": str(CUTOVER),
        "FAKE_FW_GATED": "1",
        "FAKE_LOG_SEEN": "0",
        "MONITOR_SECONDS": "1",
    }
    proc = subprocess.run(["bash", str(WRAPPER), sha, "42"], env=env, capture_output=True, text=True, timeout=90)
    assert proc.returncode == 6, proc.stdout + proc.stderr
    ipt = (cut.fake / "ipt.log").read_text().splitlines()
    assert any(" -I " in f" {line} " and "--dport 443" in line for line in ipt)
    assert _rules(cut) == []
    assert "503" in cut.edge.read_text()
    assert "state=maintenance" in cut.marker.read_text()


# ---------------------------------------------------------------------------------------------------
# Re-runnable edge render: EDGE_SOURCE is the render input, EDGE_CONF only the install target
# ---------------------------------------------------------------------------------------------------

MAINT_CONF = """# fb maintenance response written by cutover_fb_cafe24.sh
server { listen 80; server_name fb.newtalk.kr; location / { return 503 "maintenance\\n"; } }
"""
CAFE24_CONF = f"""server {{
    listen 443 ssl;
    location /api/ {{
        proxy_pass https://{CAFE24_IP};
        proxy_ssl_server_name on;
        proxy_ssl_verify on;
        proxy_ssl_verify_depth 3;
    }}
    location /dash/ {{ proxy_pass http://aads_dashboard; }}
}}
"""


def _sourced(c, body, **extra):
    """Run `body` in a set -euo pipefail shell that sourced the real cutover script (no subcommand runs)."""
    script = f"set -euo pipefail; . {CUTOVER}\n{body}"
    return subprocess.run(["bash", "-c", script], env={**c.env, **extra}, capture_output=True, text=True, timeout=60)


def _backup(c, stamp, text):
    f = c.tmp / f"edge-fb.conf.bak.pre_cafe24_cutover_{stamp}"
    f.write_text(text)
    return f


def test_sourcing_the_script_runs_no_subcommand(cut):
    proc = _sourced(cut, "declare -F cmd_lint resolve_edge_source routed_health_local >/dev/null && echo LOADED")
    assert proc.returncode == 0 and "LOADED" in proc.stdout and proc.stderr == ""


def test_lint_leaves_no_return_trap_so_later_function_returns_survive_set_u(cut):
    body = """
outer() { cmd_lint; return 0; }
outer
trap -p RETURN
after() { local x=1; return 0; }
after; after
echo SURVIVED
"""
    proc = _sourced(cut, body)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SURVIVED" in proc.stdout and "RETURN" not in proc.stdout
    assert "unbound variable" not in proc.stderr


def test_lint_cli_passes_with_the_default_fixture(cut):
    proc = _cut(cut, "lint")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mode=edge-conf" in proc.stdout


def test_overwritten_edge_conf_renders_from_the_newest_backup_that_has_the_legacy_upstream(cut):
    cut.edge.write_text(MAINT_CONF)
    _backup(cut, "20261003_090000", EDGE_FIXTURE.replace("/api/", "/api-old/"))
    newer = _backup(cut, "20261003_100000", EDGE_FIXTURE)
    _backup(cut, "20261003_110000", MAINT_CONF)  # a later backup of the maintenance conf has nothing to render
    proc = _cut(cut, "lint")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"source={newer} mode=backup" in proc.stdout
    assert cut.edge.read_text() == MAINT_CONF  # lint never writes the install target
    assert not _kind(_log(cut), "docker ")


def test_apply_edge_recovers_from_a_maintenance_conf_using_the_backup_and_never_reloads_the_old_upstream(cut):
    cut.edge.write_text(MAINT_CONF)
    _backup(cut, "20261003_100000", EDGE_FIXTURE)
    proc = _apply_edge(cut)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    body = cut.edge.read_text()
    assert f"proxy_pass https://{CAFE24_IP}" in body and "http://aads_dashboard" in body
    assert "yeoljeong_finance_api" not in body and "jinah" not in body.lower() and "503" not in body
    assert "mode=backup" in proc.stdout and "state=cafe24-verified" in cut.marker.read_text()


def test_edge_conf_that_already_points_at_cafe24_is_idempotent(cut):
    cut.edge.write_text(CAFE24_CONF)
    proc = _cut(cut, "lint")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mode=already-cafe24" in proc.stdout and cut.edge.read_text() == CAFE24_CONF


def test_already_cafe24_conf_without_chain_depth_is_rejected(cut):
    cut.edge.write_text(CAFE24_CONF.replace("proxy_ssl_verify_depth 3;", ""))
    proc = _cut(cut, "lint")
    assert proc.returncode != 0 and "proxy_ssl_verify_depth" in proc.stderr


@pytest.mark.parametrize("with_unusable_backup", [False, True])
def test_no_render_source_fails_closed_with_a_clear_message(cut, with_unusable_backup):
    cut.edge.write_text(MAINT_CONF)
    if with_unusable_backup:
        _backup(cut, "20261003_100000", MAINT_CONF)
    proc = _cut(cut, "lint")
    assert proc.returncode != 0
    assert "no edge render source" in proc.stderr and "EDGE_SOURCE" in proc.stderr
    assert not _kind(_log(cut), "docker ") and cut.edge.read_text() == MAINT_CONF


def test_apply_edge_without_a_source_fails_closed_before_touching_nginx(cut):
    cut.edge.write_text(MAINT_CONF)
    proc = _apply_edge(cut)
    assert proc.returncode != 0 and "no edge render source" in proc.stderr
    assert not _kind(_log(cut), "docker ") and "nginx switch lock held" not in proc.stdout
    assert cut.edge.read_text() == MAINT_CONF and not cut.marker.exists()


def test_explicit_edge_source_is_the_render_input_and_must_be_usable(cut):
    cut.edge.write_text(MAINT_CONF)
    good = cut.tmp / "pre-cutover.conf"
    good.write_text(EDGE_FIXTURE)
    proc = _cut(cut, "lint", EDGE_SOURCE=str(good))
    assert proc.returncode == 0 and f"source={good} mode=explicit" in proc.stdout
    junk = cut.tmp / "junk.conf"
    junk.write_text(MAINT_CONF)
    assert _cut(cut, "lint", EDGE_SOURCE=str(junk)).returncode != 0
    assert _cut(cut, "lint", EDGE_SOURCE=str(cut.tmp / "missing.conf")).returncode != 0


def test_render_adds_chain_depth_3_to_every_cafe24_location(cut):
    proc = _sourced(cut, 'render_edge "$EDGE_CONF"')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count(f"proxy_pass https://{CAFE24_IP}") == 1
    assert re.search(r"^\s*proxy_ssl_verify_depth 3;$", proc.stdout, re.M)
    assert proc.stdout.count("proxy_ssl_verify_depth") == proc.stdout.count("proxy_ssl_verify on")
    assert "http://aads_dashboard" in proc.stdout and "yeoljeong_finance_api" not in proc.stdout


def test_lint_rejects_a_render_without_verify_depth(cut):
    body = """
render_edge() { sed -E '/proxy_ssl_verify_depth/d' < <(awk -v ip="$CAFE24_IP" '/yeoljeong_finance_api/{sub(/http:\\/\\/yeoljeong_finance_api/,"https://" ip); print; print "proxy_ssl_verify on;"; next}{print}' "$1"); }
EDGE_SRC="$EDGE_CONF"; lint_edge_render
"""
    proc = _sourced(cut, body)
    assert proc.returncode != 0 and "proxy_ssl_verify_depth" in proc.stderr


def test_lint_requires_the_chain_file_in_the_apache_443_block(cut):
    tpl = (ROOT / "config/apache/fb-cafe24.conf").read_text()
    assert re.search(r"^\s*SSLCertificateChainFile\s+\S+", tpl, re.M)
    assert _cut(cut, "lint").returncode == 0
    no_chain = cut.tmp / "no-chain.conf"
    no_chain.write_text(re.sub(r"^\s*SSLCertificateChainFile.*\n", "", tpl, flags=re.M))
    proc = _cut(cut, "lint", TEMPLATE=str(no_chain))
    assert proc.returncode != 0 and "SSLCertificateChainFile" in proc.stderr
    # a chain file only in the :80 block (or only commented out) must not satisfy the :443 check
    commented = cut.tmp / "commented.conf"
    commented.write_text(re.sub(r"^(\s*)(SSLCertificateChainFile)", r"\1# \2", tpl, flags=re.M))
    assert _cut(cut, "lint", TEMPLATE=str(commented)).returncode != 0


def test_routed_health_local_ignores_the_expired_edge_certificate(cut):
    proc = _apply_edge(cut)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    routed = [line for line in _kind(_log(cut), "curl kind=routed")]
    assert routed and all("insecure=1" in line for line in routed), routed
    probe = [line for line in routed if "probe=" in line]
    assert len(probe) == 1 and probe[0].endswith("lock=held")
    direct = _kind(_log(cut), "curl kind=direct")
    assert direct and all("insecure=0" in line for line in direct)  # cafe24 origin is verified normally
