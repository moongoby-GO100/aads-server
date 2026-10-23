"""Credential-lock lifecycle: one refresh owner per slot, no lock across a model call.

Real subprocesses only.  The wrappers run for real against a fake `claude`
binary / fake `docker`, and the refresh responder behaves like a single-use
refresh-grant server (a grant presented twice is rejected and logged as `reuse`).

The fake docker reproduces the daemon/client split: `docker exec` starts a
supervisor in its own session with a clean environment (the "daemon"), the
container command runs under that supervisor, and killing the `docker exec`
client does NOT stop it.  No real OAuth endpoint, credential path, container or
paid model is touched.
"""
import fcntl
import glob
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
HOST_WRAPPER = SCRIPTS / "claude-slot-credentials-wrapper.sh"
DOCKER_WRAPPER = SCRIPTS / "claude-docker-wrapper.sh"
HELPER = SCRIPTS / "claude_slot_credential.py"
TAG = "AADS_CLAUDE_CALL_ID"

ENGINE = r'''
import fcntl, json, os, subprocess, sys, time
from pathlib import Path

CFG = Path(os.environ["FAKE_DIR"])


def log(event, **extra):
    with open(CFG / "events.log", "a") as handle:
        handle.write(json.dumps({"ev": event, "pid": os.getpid(), "t": time.time(), **extra}) + "\n")


def write_cred(path, generation, expires_ms, with_refresh=True):
    oauth = {"accessToken": "access-g%d" % generation, "expiresAt": expires_ms}
    if with_refresh:
        oauth["refreshToken"] = "refresh-g%d" % generation
    path.write_text(json.dumps({"claudeAiOauth": oauth, "generation": generation}))
    os.chmod(path, 0o600)


def rotate(path, payload):
    """Single-use grant server: presenting the same refreshToken twice is rejected."""
    with open(CFG / "used.lock", "a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        used_path = CFG / "used.json"
        used = json.loads(used_path.read_text()) if used_path.exists() else []
        token = payload["claudeAiOauth"]["refreshToken"]
        if token in used:
            log("reuse", token=token)
            return False
        used.append(token)
        used_path.write_text(json.dumps(used))
    write_cred(path, int(payload.get("generation", 0)) + 1, int((time.time() + 28800) * 1000))
    return True


def spawn_descendants():
    pythons = [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]
    mode = os.environ["FAKE_SPAWN"]
    if "a" in mode:  # new session AND cleared env: only the parent walk can find it
        child = subprocess.Popen(pythons, env={"PATH": os.environ["PATH"]}, start_new_session=True)
        (CFG / "gc.a").write_text(str(child.pid))
    if "b" in mode:  # double fork + setsid, env inherited: only the tag can find it
        pid = os.fork()
        if pid == 0:
            os.setsid()
            if os.fork() == 0:
                (CFG / "gc.b").write_text(str(os.getpid()))
                time.sleep(60)
            os._exit(0)
        os.waitpid(pid, 0)
    if "c" in mode:  # plain tool child
        child = subprocess.Popen(pythons)
        (CFG / "gc.c").write_text(str(child.pid))
    deadline = time.time() + 5
    while time.time() < deadline and not all((CFG / ("gc." + k)).exists() for k in mode):
        time.sleep(0.05)


def main(home, argv):
    cred = Path(home) / ".claude" / ".credentials.json"
    payload = json.loads(cred.read_text())
    oauth = payload["claudeAiOauth"]
    if argv[:2] == ["-p", "ping"]:
        log("refresh", home=str(home), tag=os.environ.get("AADS_CLAUDE_CALL_ID", ""))
        if "refreshToken" not in oauth:
            log("refresh_without_grant")
            return 1
        time.sleep(float(os.environ.get("FAKE_REFRESH_SLEEP", "0")))
        mode = os.environ.get("FAKE_REFRESH_MODE", "ok")
        if mode == "fail":
            return 1
        if mode == "invalid":
            oauth.update(accessToken="", refreshToken="")
            cred.write_text(json.dumps(payload))
            return 0
        return 0 if rotate(cred, payload) else 1
    if argv[:1] == ["echo"]:
        sys.stdout.write(sys.stdin.read().upper())
        return 7
    log("call_start", expires=oauth["expiresAt"], access=oauth["accessToken"],
        has_refresh="refreshToken" in oauth, tag=os.environ.get("AADS_CLAUDE_CALL_ID", ""))
    (CFG / ("started.%d" % os.getpid())).write_text("1")
    if os.environ.get("FAKE_SPAWN"):
        spawn_descendants()
    go = os.environ.get("FAKE_LEGACY_SELF_REFRESH_GO")
    if go:  # what the real CLI did before: redeem the refreshToken it was handed
        deadline = time.time() + 20
        while not os.path.exists(go) and time.time() < deadline:
            time.sleep(0.05)
        if "refreshToken" in json.loads(cred.read_text())["claudeAiOauth"]:
            log("self_refresh")
            rotate(cred, payload)
        else:
            log("no_refresh_token")
    want = os.environ.get("FAKE_EXPECT_ACCESS")
    end = time.time() + float(os.environ.get("FAKE_CALL_SLEEP", "0"))
    seen = oauth["accessToken"]
    while time.time() < end or (want and seen != want and time.time() < end + 10):
        time.sleep(0.05)
        try:
            current = json.loads(cred.read_text())["claudeAiOauth"]["accessToken"]
        except (OSError, ValueError, KeyError):
            continue
        if current != seen:
            seen = current
            log("access_seen", access=seen)
        if want and seen == want and time.time() >= end:
            break
    log("call_end")
    if want and seen != want:
        return 3
    return int(os.environ.get("FAKE_CALL_EXIT", "0"))
'''

FAKE_CLAUDE = """#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.environ["FAKE_DIR"])
import engine
sys.exit(engine.main(os.environ["HOME"], sys.argv[1:]))
"""

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, re, shutil, subprocess, sys
from pathlib import Path

root = Path(os.environ["FAKE_DOCKER_ROOT"])
here = Path(__file__).resolve().parent
args = sys.argv[1:]

SUPERVISOR = """
import json, os, subprocess, sys
spec = json.loads(sys.argv[1])
code = subprocess.call(spec["argv"], env=spec["env"])
try:
    os.write(int(sys.argv[2]), str(code).encode())
except OSError:
    pass
"""


def mapped(container_path):
    return root / container_path.lstrip("/")


if args[0] == "cp":
    source, destination = args[1], args[2]
    try:
        if ":" in source and not source.startswith("/"):
            shutil.copy2(mapped(source.split(":", 1)[1]), destination)
        else:
            target = mapped(destination.split(":", 1)[1])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    except FileNotFoundError:
        raise SystemExit(1)
    raise SystemExit(0)

if args[0] != "exec":
    raise SystemExit(2)
index, interactive = 1, False
if args[index] == "-i":
    interactive = True
    index += 1
env_in = {}
while index < len(args) and args[index] == "-e":
    key, value = args[index + 1].split("=", 1)
    env_in[key] = value
    index += 2
index += 1  # container name
command = args[index:]
with open(os.path.join(os.environ["FAKE_DIR"], "docker.log"), "a") as record:
    record.write(json.dumps(command) + "\n")

if command[:2] == ["sh", "-lc"]:
    found = re.search(r"'(/tmp/[^']+)'", command[2])
    target = found.group(1) if found else None
    if target and "mkdir -p" in command[2]:
        mapped(target).mkdir(parents=True, exist_ok=True)
    elif target and "rm -rf" in command[2]:
        shutil.rmtree(mapped(target), ignore_errors=True)
    elif target and "rm -f" in command[2]:
        mapped(target).unlink(missing_ok=True)
    raise SystemExit(0)
if command[0] == "mv":
    os.replace(mapped(command[-2]), mapped(command[-1]))
    raise SystemExit(0)

if command[0] == "timeout":
    command = command[1:]
    if command[0] == "-k":
        command = command[2:]
    command = command[1:]
if command[:2] == ["python3", "-"]:
    argv = [sys.executable] + command[1:]
    child_env = {"PATH": os.environ["PATH"]}
else:
    argv = [str(here / "claude")] + command[1:]
    child_env = {"PATH": os.environ["PATH"], "FAKE_DIR": os.environ["FAKE_DIR"]}
    for key in ("FAKE_CALL_SLEEP", "FAKE_REFRESH_SLEEP", "FAKE_REFRESH_MODE", "FAKE_SPAWN",
                "FAKE_LEGACY_SELF_REFRESH_GO", "FAKE_EXPECT_ACCESS", "FAKE_CALL_EXIT"):
        if key in os.environ:
            child_env[key] = os.environ[key]
child_env.update(env_in)
if "HOME" in env_in:
    child_env["HOME"] = str(mapped(env_in["HOME"]))

read_end, write_end = os.pipe()
daemon_env = {"PATH": os.environ["PATH"]}
stdin = 0 if interactive else subprocess.DEVNULL
subprocess.Popen(
    [sys.executable, "-c", SUPERVISOR, json.dumps({"argv": argv, "env": child_env}), str(write_end)],
    env=daemon_env, start_new_session=True, pass_fds=[write_end], stdin=stdin,
)
os.close(write_end)
data = os.read(read_end, 32)
code = int(data) if data else 137
raise SystemExit(code if code >= 0 else 128 - code)
'''


def load_helper():
    spec = importlib.util.spec_from_file_location("claude_slot_credential_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_credential(path: Path, *, expires_in: float, generation: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "access-g%d" % generation,
            "refreshToken": "refresh-g%d" % generation,
            "expiresAt": int((time.time() + expires_in) * 1000),
        },
        "generation": generation,
    }))
    path.chmod(0o600)


def pid_alive(pid: int) -> bool:
    try:
        stat = Path("/proc/%d/stat" % pid).read_text()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2:].split()[0] != "Z"


def tagged_alive(tag: str) -> list:
    needle = ("%s=%s" % (TAG, tag)).encode()
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            environ = Path("/proc/%s/environ" % name).read_bytes()
        except OSError:
            continue
        if needle in environ.split(b"\0") and pid_alive(int(name)):
            found.append(int(name))
    return found


class Rig:
    def __init__(self, tmp_path: Path, slot: str, kind: str):
        self.tmp = tmp_path
        self.slot = slot
        self.kind = kind
        self.fake_dir = tmp_path / "fake"
        self.fake_dir.mkdir()
        (self.fake_dir / "engine.py").write_text(ENGINE)
        self.cred = tmp_path / "slot" / ".claude" / ".credentials.json"
        self.lock = self.cred.parent / ".credentials.lock"
        self.container_root = tmp_path / "container"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self.fake_claude = bin_dir / "claude"
        self.fake_claude.write_text(FAKE_CLAUDE)
        self.fake_claude.chmod(0o755)
        self.fake_docker = bin_dir / "docker"
        self.fake_docker.write_text(FAKE_DOCKER)
        self.fake_docker.chmod(0o755)
        self.env = dict(os.environ)
        self.env.update({
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "FAKE_DIR": str(self.fake_dir),
            "FAKE_DOCKER_ROOT": str(self.container_root),
            "CLAUDE_SLOT_CREDENTIALS_FILE": str(self.cred),
            "CLAUDE_SLOT_CREDENTIAL_LOCK_FILE": str(self.lock),
            "CLAUDE_OAUTH_SLOT": slot,
            "CLAUDE_SLOT_REFRESH_BIN": str(self.fake_claude),
            "CLAUDE_SLOT_WATCH_INTERVAL_SEC": "0.2",
            "CLAUDE_SLOT_REAP_GRACE_SEC": "2",
            "CLAUDE_SLOT_REAP_KILL_WAIT_SEC": "2",
            "CLAUDE_SLOT_WATCH_STOP_WAIT_SEC": "8",
        })
        self.env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        self.env.pop(TAG, None)

    def argv(self, *call_args):
        if self.kind == "host":
            return [str(HOST_WRAPPER), str(self.fake_claude), *call_args]
        return [str(DOCKER_WRAPPER), *call_args]

    def popen(self, *call_args, **env_extra):
        env = dict(self.env, **env_extra)
        return subprocess.Popen(
            self.argv(*call_args), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def events(self, name=None):
        path = self.fake_dir / "events.log"
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines() if line]
        return [row for row in rows if name is None or row["ev"] == name]

    def container_reaps(self):
        path = self.fake_dir / "docker.log"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if row[:2] == ["python3", "-"] and "reap" in row]

    def descendants(self):
        pids = {}
        for kind in "abc":
            path = self.fake_dir / ("gc." + kind)
            if path.exists():
                pids[kind] = int(path.read_text())
        return pids

    def wait_for(self, predicate, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def lock_is_free(self) -> bool:
        if not self.lock.exists():
            return True
        with self.lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            return True

    def leftovers(self):
        return (
            glob.glob("/tmp/claude-slot-%s.*" % self.slot)
            + glob.glob("/tmp/claude-slot-state-%s.*" % self.slot)
            + [str(p) for p in self.container_root.glob("tmp/.claude-relay-slot-*")]
            + [str(p) for p in self.cred.parent.glob("*.tmp")]
        )

    def residual_processes(self):
        """Every model/tool/refresh process of every call this rig made that is still alive."""
        alive = []
        for event in self.events("call_start") + self.events("refresh"):
            if pid_alive(event["pid"]):
                alive.append(("pid", event["pid"]))
            if event.get("tag"):
                alive.extend(("tag", pid) for pid in tagged_alive(event["tag"]))
        alive.extend(("descendant", pid) for pid in self.descendants().values() if pid_alive(pid))
        return alive

    def credential(self):
        return json.loads(self.cred.read_text())


@pytest.fixture(params=["host", "docker"])
def rig(request, tmp_path):
    slot = "lc%s%d" % (request.param[0], os.getpid())
    instance = Rig(tmp_path, slot, request.param)
    yield instance
    residual = instance.residual_processes()
    for _, pid in residual:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert not residual, residual
    assert not instance.leftovers(), instance.leftovers()


def finish(process, timeout=30):
    out, err = process.communicate(timeout=timeout)
    return process.returncode, out, err


# ── lock lifecycle (kept from the first revision) ─────────────────────────

def test_long_call_does_not_hold_lock_or_block_next_request(rig):
    write_credential(rig.cred, expires_in=28800)
    first = rig.popen("call", FAKE_CALL_SLEEP="5.3")
    assert rig.wait_for(lambda: rig.events("call_start"))
    assert rig.lock_is_free(), "model call must not hold the credential lock"

    started = time.time()
    second = rig.popen("call", FAKE_CALL_SLEEP="0")
    code, _, err = finish(second, timeout=15)
    elapsed = time.time() - started

    assert code == 0, err
    assert first.poll() is None, "second request must complete while the long call is still running"
    assert elapsed < 4.5
    code, _, err = finish(first)
    assert code == 0, err
    assert len(rig.events("call_start")) == 2
    assert rig.events("refresh") == []


def test_near_expiry_refresh_has_one_owner_and_calls_run_unlocked(rig):
    write_credential(rig.cred, expires_in=200)
    processes = [
        rig.popen("call", FAKE_REFRESH_SLEEP="0.8", FAKE_CALL_SLEEP="1.5")
        for _ in range(4)
    ]
    assert rig.wait_for(lambda: len(rig.events("call_start")) >= 1, timeout=20)
    results = [finish(p) for p in processes]

    assert [r[0] for r in results] == [0, 0, 0, 0], results
    assert len(rig.events("refresh")) == 1, "exactly one refresher"
    assert rig.events("reuse") == [], "no refresh grant may be presented twice"
    assert len(rig.events("call_start")) == 4
    horizon = (time.time() + 3600) * 1000
    assert all(event["expires"] > horizon for event in rig.events("call_start"))
    assert {event["access"] for event in rig.events("call_start")} == {"access-g1"}
    assert rig.credential()["generation"] == 1
    assert oct(rig.cred.stat().st_mode & 0o777) == "0o600"
    for _, _, err in results:
        assert "access-g" not in err and "refresh-g" not in err


def test_refresh_lock_is_held_only_for_the_refresh(rig):
    write_credential(rig.cred, expires_in=200)
    proc = rig.popen("call", FAKE_REFRESH_SLEEP="0.5", FAKE_CALL_SLEEP="3")
    assert rig.wait_for(lambda: rig.events("call_start"), timeout=20)
    assert rig.lock_is_free()
    code, _, err = finish(proc)
    assert code == 0, err


def test_busy_lock_fails_fast_with_internal_wait_marker_not_auth_marker(rig):
    rig.env.pop("CLAUDE_SLOT_CREDENTIAL_LOCK_FILE", None)
    write_credential(rig.cred, expires_in=200)
    rig.lock.parent.mkdir(parents=True, exist_ok=True)
    with rig.lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = rig.popen("call", CLAUDE_SLOT_REFRESH_LOCK_WAIT_SEC="0.2")
        code, _, err = finish(proc, timeout=10)

    from app.core.claude_oauth_credentials import classify_auth_error

    assert code == 75
    assert "oauth_slot_busy" in err
    assert classify_auth_error(err) == "error"
    assert not rig.events("call_start") and not rig.events("refresh")
    assert "access-g" not in err


def test_valid_credential_ignores_a_held_lock(rig):
    write_credential(rig.cred, expires_in=28800)
    rig.lock.parent.mkdir(parents=True, exist_ok=True)
    with rig.lock.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, _, err = finish(rig.popen("call"), timeout=15)
    assert code == 0, err


@pytest.mark.parametrize("mode", ["fail", "invalid"])
def test_failed_or_invalid_refresh_keeps_credential_and_recovers(rig, mode):
    write_credential(rig.cred, expires_in=-30)
    before = rig.cred.read_bytes()
    code, _, err = finish(rig.popen("call", FAKE_REFRESH_MODE=mode), timeout=30)

    assert code == 76
    assert "oauth_slot_unavailable" in err
    assert not rig.events("call_start")
    assert rig.cred.read_bytes() == before, "a failed refresh must never touch the authoritative file"
    assert rig.lock_is_free()

    code, _, err = finish(rig.popen("call", CLAUDE_SLOT_REFRESH_RETRY_SEC="0"), timeout=30)
    assert code == 0, err
    assert rig.credential()["generation"] == 1


def test_refresh_retries_are_rate_limited_per_credential(rig):
    write_credential(rig.cred, expires_in=200)
    code, _, err = finish(rig.popen("call", FAKE_REFRESH_MODE="fail"), timeout=30)
    assert code == 0, err  # still valid: the call proceeds with the current token
    code, _, err = finish(rig.popen("call", FAKE_REFRESH_MODE="fail"), timeout=30)
    assert code == 0, err
    assert len(rig.events("refresh")) == 1, "no refresh storm after a failed attempt"


# ── blocking issue 1: single refresh ownership ────────────────────────────

def test_model_cli_never_receives_the_refresh_grant(rig):
    write_credential(rig.cred, expires_in=200)
    code, _, err = finish(rig.popen("call", FAKE_REFRESH_SLEEP="0.2"), timeout=30)
    assert code == 0, err
    calls = rig.events("call_start")
    assert calls and all(event["has_refresh"] is False for event in calls)
    assert "refreshToken" in rig.cred.read_text(), "the authoritative file keeps its grant"
    assert rig.events("reuse") == []


def test_rig_detects_a_double_redemption_of_one_grant(tmp_path):
    """Control: the harness must flag the original bug, or the zero-reuse asserts prove nothing."""
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "engine.py").write_text(ENGINE)
    env = dict(os.environ, FAKE_DIR=str(fake))
    script = (
        "import sys, json; sys.path.insert(0, %r); import engine, pathlib\n"
        "p = pathlib.Path(%r); engine.write_cred(p, 0, 1)\n"
        "payload = json.loads(p.read_text())\n"
        "print(engine.rotate(p, payload), engine.rotate(p, payload))\n" % (str(fake), str(tmp_path / "c.json"))
    )
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert out.stdout.split() == ["True", "False"], out
    assert [json.loads(line)["ev"] for line in (fake / "events.log").read_text().splitlines()] == ["reuse"]


def test_concurrent_long_calls_with_cli_self_refresh_attempt_redeem_the_grant_zero_times(rig):
    """Reviewer's reproduction: expires_in=1000, two concurrent calls, sequential self-refresh, 2s calls."""
    write_credential(rig.cred, expires_in=1000)
    go = rig.tmp / "go"
    processes = [
        rig.popen("call", FAKE_LEGACY_SELF_REFRESH_GO=str(go), FAKE_CALL_SLEEP="2",
                  CLAUDE_SLOT_WATCH_INTERVAL_SEC="10")
        for _ in range(2)
    ]
    assert rig.wait_for(lambda: len(rig.events("call_start")) == 2, timeout=20)
    go.write_text("1")
    results = [finish(p) for p in processes]

    assert [r[0] for r in results] == [0, 0], results
    assert rig.events("reuse") == []
    assert rig.events("self_refresh") == []
    assert len(rig.events("no_refresh_token")) == 2
    assert rig.credential()["generation"] == 0


def test_expiry_during_a_long_call_is_refreshed_once_and_pushed_to_every_call(rig):
    write_credential(rig.cred, expires_in=8)
    env = dict(
        CLAUDE_SLOT_REFRESH_LOCK_WINDOW_SEC="5", FAKE_REFRESH_SLEEP="0.4", FAKE_CALL_SLEEP="6",
        FAKE_EXPECT_ACCESS="access-g1",
    )
    processes = [rig.popen("call", **env) for _ in range(2)]
    results = [finish(p, timeout=60) for p in processes]

    assert [r[0] for r in results] == [0, 0], results
    assert len(rig.events("refresh")) == 1, "one owner across both calls' watchers"
    assert rig.events("reuse") == []
    assert {e["access"] for e in rig.events("access_seen")} == {"access-g1"}
    assert len(rig.events("access_seen")) == 2
    assert rig.credential()["generation"] == 1


def test_keeper_style_writer_under_the_shared_lock_is_not_overwritten_or_redeemed_twice(rig, monkeypatch):
    rig.env.pop("CLAUDE_SLOT_CREDENTIAL_LOCK_FILE", None)
    write_credential(rig.cred, expires_in=8)
    monkeypatch.setenv("FAKE_DIR", str(rig.fake_dir))
    sys.path.insert(0, str(rig.fake_dir))
    try:
        import importlib

        engine = importlib.import_module("engine")
        importlib.reload(engine)
        proc = rig.popen("call", CLAUDE_SLOT_REFRESH_LOCK_WINDOW_SEC="5", FAKE_CALL_SLEEP="6",
                         FAKE_EXPECT_ACCESS="access-g1")
        assert rig.wait_for(lambda: rig.events("call_start"), timeout=20)
        with rig.lock.open("a") as handle:  # the keeper takes the same flock around its refresh
            fcntl.flock(handle, fcntl.LOCK_EX)
            time.sleep(3.2)  # watcher ticks while the keeper holds it; it must wait, not redeem
            payload = json.loads(rig.cred.read_text())
            assert engine.rotate(rig.cred, payload)
            fcntl.flock(handle, fcntl.LOCK_UN)
        code, _, err = finish(proc, timeout=60)
    finally:
        sys.path.remove(str(rig.fake_dir))
        sys.modules.pop("engine", None)

    assert code == 0, err
    assert rig.events("refresh") == [], "the watcher must see the keeper's result instead of redeeming"
    assert rig.events("reuse") == []
    assert rig.credential()["generation"] == 1
    assert {e["access"] for e in rig.events("access_seen")} == {"access-g1"}


def test_commit_is_a_fenced_compare_and_swap(tmp_path):
    helper = load_helper()
    cred = tmp_path / ".credentials.json"
    write_credential(cred, expires_in=100)
    base = helper.read_status(cred)
    newer = tmp_path / "newer.json"
    write_credential(newer, expires_in=9000, generation=1)

    assert helper.commit(cred, newer.read_bytes(), "not-the-current-fingerprint")[0] == helper.STALE
    older = tmp_path / "older.json"
    write_credential(older, expires_in=50, generation=2)
    assert helper.commit(cred, older.read_bytes(), base.fingerprint)[0] == helper.NOT_NEWER
    assert helper.commit(cred, b"{}", base.fingerprint)[0] == helper.INVALID
    assert helper.read_status(cred).fingerprint == base.fingerprint
    assert helper.commit(cred, cred.read_bytes(), base.fingerprint)[0] == helper.UNCHANGED
    assert helper.commit(cred, newer.read_bytes(), base.fingerprint)[0] == helper.COMMITTED
    assert json.loads(cred.read_text())["generation"] == 1
    assert helper.strip_refresh(cred.read_bytes()) is not None
    assert b"refreshToken" not in helper.strip_refresh(cred.read_bytes())


@pytest.mark.parametrize("mode", ["host", "docker"])
def test_sigterm_during_refresh_still_commits_the_consumed_grant(tmp_path, mode):
    rig = Rig(tmp_path, "dl%s%d" % (mode[0], os.getpid()), mode)
    write_credential(rig.cred, expires_in=200)
    argv = [
        sys.executable, str(HELPER), "prepare", "--mode", mode,
        "--cred", str(rig.cred), "--lock", str(rig.lock), "--slot", rig.slot,
        "--state", str(tmp_path / "state.json"), "--snapshot", str(tmp_path / "snap.json"),
        "--refresh-bin", str(rig.fake_claude), "--container", "aads-server",
    ]
    proc = subprocess.Popen(argv, env=dict(rig.env, FAKE_REFRESH_SLEEP="1.5"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert rig.wait_for(lambda: rig.events("refresh"), timeout=15)
    proc.send_signal(signal.SIGTERM)
    code, _, err = finish(proc, timeout=30)

    assert code == 143, err
    assert rig.credential()["generation"] == 1, "tokens from the redeemed grant must be saved"
    assert rig.events("reuse") == []
    assert rig.lock_is_free()
    assert rig.residual_processes() == []
    assert not rig.leftovers()


@pytest.mark.parametrize("mode", ["host", "docker"])
def test_cancel_during_a_stuck_refresh_reaps_the_refresh_process(tmp_path, mode):
    rig = Rig(tmp_path, "st%s%d" % (mode[0], os.getpid()), mode)
    write_credential(rig.cred, expires_in=200)
    argv = [
        sys.executable, str(HELPER), "prepare", "--mode", mode,
        "--cred", str(rig.cred), "--lock", str(rig.lock), "--slot", rig.slot,
        "--state", str(tmp_path / "state.json"), "--snapshot", str(tmp_path / "snap.json"),
        "--refresh-bin", str(rig.fake_claude), "--container", "aads-server", "--refresh-timeout", "30",
    ]
    env = dict(rig.env, FAKE_REFRESH_SLEEP="40", CLAUDE_SLOT_SIGNAL_GRACE_SEC="1")
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert rig.wait_for(lambda: rig.events("refresh"), timeout=15)
    proc.send_signal(signal.SIGTERM)
    code, _, err = finish(proc, timeout=40)

    assert code == 143, err
    assert rig.wait_for(lambda: rig.residual_processes() == [], timeout=5), rig.residual_processes()
    assert rig.lock_is_free()
    assert not rig.leftovers()


# ── blocking issue 2: per-call process cleanup ────────────────────────────

@pytest.mark.parametrize("how,spawn", [("sigterm", "abc"), ("timeout", "bc")])
def test_cancellation_reaps_model_and_tool_descendants_and_next_request_proceeds(rig, how, spawn):
    write_credential(rig.cred, expires_in=200)
    env = dict(FAKE_REFRESH_SLEEP="0.3", FAKE_CALL_SLEEP="30", FAKE_SPAWN=spawn)
    if how == "sigterm":
        proc = rig.popen("call", **env)
        assert rig.wait_for(lambda: len(rig.descendants()) == len(spawn), timeout=20)
        proc.send_signal(signal.SIGTERM)
        code, _, err = finish(proc, timeout=30)
        assert code == 143, err
    else:
        proc = subprocess.Popen(
            ["timeout", "-k", "10", "5", *rig.argv("call")], env=dict(rig.env, **env),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        code, _, err = finish(proc, timeout=40)
        assert code == 124, err

    assert rig.lock_is_free()
    assert len(rig.descendants()) == len(spawn)
    if rig.kind == "docker":
        # The fake shares the host PID space, so a host-side scan could also find the
        # "container" processes; assert the wrapper really ran the in-container reap.
        tag = rig.events("call_start")[0]["tag"]
        assert any(tag in row for row in rig.container_reaps()), rig.container_reaps()
    assert rig.wait_for(lambda: rig.residual_processes() == [], timeout=5), rig.residual_processes()
    assert not rig.leftovers()

    code, _, err = finish(rig.popen("call", FAKE_CALL_SLEEP="0"), timeout=30)
    assert code == 0, err


def test_killing_only_the_docker_client_leaves_the_container_process_until_reap(tmp_path):
    """The daemon/client split the first revision's fake docker hid: the client PID is not the call."""
    rig = Rig(tmp_path, "cl%d" % os.getpid(), "docker")
    home = rig.container_root / "tmp" / "h"
    write_credential(home / ".claude" / ".credentials.json", expires_in=28800)
    tag = uuid.uuid4().hex
    client = subprocess.Popen(
        [str(rig.fake_docker), "exec", "-i", "-e", "%s=%s" % (TAG, tag), "-e", "HOME=/tmp/h",
         "aads-server", "/app/scripts/claude-oauth-wrapper.sh", "call"],
        env=dict(rig.env, FAKE_CALL_SLEEP="30", FAKE_SPAWN="bc"),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert rig.wait_for(lambda: rig.events("call_start") and len(rig.descendants()) == 2, timeout=20)
        model_pid = rig.events("call_start")[0]["pid"]
        client.kill()
        client.wait(timeout=5)
        time.sleep(0.5)
        assert pid_alive(model_pid), "killing the docker client must not (and in real docker does not) stop the call"
        assert all(pid_alive(pid) for pid in rig.descendants().values())

        done = subprocess.run(
            [sys.executable, str(HELPER), "reap", "--call-id", tag, "--container", "aads-server",
             "--grace", "2", "--kill-wait", "2"],
            env=rig.env, capture_output=True, text=True, timeout=40,
        )
        assert done.returncode == 0, done.stderr
        assert rig.wait_for(lambda: rig.residual_processes() == [], timeout=5), rig.residual_processes()
    finally:
        for pid in [e["pid"] for e in rig.events("call_start")] + list(rig.descendants().values()):
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_reap_touches_only_the_tagged_call(tmp_path):
    mine, other = uuid.uuid4().hex, uuid.uuid4().hex
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    base = {"PATH": os.environ["PATH"]}
    victim = subprocess.Popen(sleeper, env=dict(base, **{TAG: mine}))
    bystander_tagged = subprocess.Popen(sleeper, env=dict(base, **{TAG: other}))
    bystander_plain = subprocess.Popen(sleeper, env=base)
    short = subprocess.Popen(sleeper, env=dict(base, **{TAG: "abc"}))
    try:
        time.sleep(0.3)
        done = subprocess.run([sys.executable, str(HELPER), "reap", "--call-id", mine, "--grace", "2",
                               "--kill-wait", "2"], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        assert victim.wait(timeout=5) is not None
        assert bystander_tagged.poll() is None and bystander_plain.poll() is None

        refused = subprocess.run([sys.executable, str(HELPER), "reap", "--call-id", "abc"],
                                 capture_output=True, text=True, timeout=30)
        assert refused.returncode == 1 and "refused" in refused.stderr
        assert short.poll() is None
    finally:
        for process in (victim, bystander_tagged, bystander_plain, short):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


# ── contracts that must not change ────────────────────────────────────────

def test_exit_code_stdin_and_stdout_pass_through(rig):
    write_credential(rig.cred, expires_in=28800)
    proc = subprocess.Popen(
        rig.argv("echo"), env=rig.env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    out, err = proc.communicate("hello stream\n", timeout=15)
    assert proc.returncode == 7, err
    assert out == "HELLO STREAM\n"
    assert err == ""


def test_missing_credential_still_exits_78(rig):
    code, _, err = finish(rig.popen("call"), timeout=10)
    assert code == 78
    assert "missing" in err


def test_default_lock_callers_share_keeper_relay_contract():
    keeper = (SCRIPTS / "claude_token_keeper.sh").read_text()
    relay = (SCRIPTS / "claude_relay_server.py").read_text()
    runner = (SCRIPTS / "pipeline-runner.sh").read_text()
    assert 'lock="${home}/.claude/.credentials.lock"' in keeper
    assert '.with_name(".credentials.lock")' in relay
    assert "CLAUDE_SLOT_CREDENTIALS_FILE=" in runner
    assert "claude-slot-credentials-wrapper.sh" in runner
    for wrapper in (HOST_WRAPPER, DOCKER_WRAPPER):
        assert '$(dirname -- "$CREDENTIAL_FILE")/.credentials.lock' in wrapper.read_text()


def test_reap_remembers_reparented_setsid_child_without_environment(tmp_path):
    spec = importlib.util.spec_from_file_location("reaper", HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    tag = uuid.uuid4().hex
    ready = tmp_path / "ready"
    child_code = (
        "import signal,time,os,pathlib; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "pathlib.Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(ready)
    )
    parent_code = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r],"
        "env={'PATH':%r},start_new_session=True); time.sleep(30)"
        % (child_code, os.environ["PATH"])
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code], env={TAG: tag, "PATH": os.environ["PATH"]})
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        child = int(ready.read_text())
        assert {parent.pid, child} <= helper._call_tree(tag)
        assert helper.reap_tag(tag, grace=.3, kill_wait=.5) == 0
        assert parent.wait(timeout=3) == -signal.SIGTERM
        assert not pid_alive(child)
        assert bystander.poll() is None
    finally:
        if child is None and ready.exists():
            child = int(ready.read_text())
        if child is not None and pid_alive(child):
            os.kill(child, signal.SIGKILL)
        for process in (parent, bystander):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_reap_pid_reuse_is_not_signalled(monkeypatch):
    spec = importlib.util.spec_from_file_location("reaper_identity", HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    sent = []
    closed = []
    monkeypatch.setattr(helper.os, "pidfd_open", lambda pid: 42)
    monkeypatch.setattr(helper.os, "close", closed.append)
    monkeypatch.setattr(helper, "_proc_table", lambda: {999: (1, "S", 200)})
    monkeypatch.setattr(helper.signal, "pidfd_send_signal", lambda *args: sent.append(args))
    helper._signal_all({999: 100}, signal.SIGKILL)
    assert sent == []
    assert closed == [42]
    helper._signal_all({999: 200}, signal.SIGTERM)
    assert sent == [(42, signal.SIGTERM)]


def test_reap_does_not_hide_signal_failure(monkeypatch):
    spec = importlib.util.spec_from_file_location("reaper_failure", HELPER)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper, "_proc_table", lambda: {999: (1, "S", 200)})
    monkeypatch.setattr(helper, "_call_tree", lambda *args: {999})
    monkeypatch.setattr(helper, "_signal_all", lambda *args: None)
    assert helper.reap_tag("a" * 32, grace=0, kill_wait=0) == 1
