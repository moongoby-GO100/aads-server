#!/usr/bin/env python3
"""Claude slot OAuth credential lifecycle: single refresh ownership per slot.

One refresh grant (refreshToken) may be consumed exactly once.  Every writer that
could consume it -- the model CLI's own self-refresh, the keeper, a sync -- used to
race on it, so the same grant was redeemed twice.  The rule here:

  * The model CLI never receives the refreshToken.  The per-call private
    credential is a snapshot with refreshToken/refreshTokenExpiresAt removed
    (verified in CLI 2.1.280: without a refreshToken the CLI takes its
    "no_refresh_token" path before any lock or network call, and re-reads the file
    when its mtime changes), so it cannot redeem a grant.
  * The grant is redeemed only by this helper (prepare/watch) while it holds the
    shared flock on the slot lock file -- the same file the keeper and the relay use.
    The lock is held for the refresh only, never for a model call.
  * Results go through commit(): a compare-and-swap on a token fingerprint with a
    strictly-later-expiry check, so a stale result never overwrites a newer one.

Sub-commands:

  prepare  Before the call: if the token is near expiry take the lock, refresh once
           (re-reading the authoritative file first), then write the stripped
           snapshot for the call.  Lock busy -> exit 75 (oauth_slot_busy).
  watch    During the call: refresh when the authoritative token nears expiry and
           push the new accessToken into the call's private credential.
  reap     After the call: terminate every process carrying the per-call tag
           AADS_CLAUDE_CALL_ID (and its descendants), bounded TERM then KILL; with
           --container the same is done inside the container first.  Untagged
           processes are never touched.

Nothing here prints token values; stdout is never used.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

EX_BUSY = 75
EX_REFRESH_FAILED = 76
EX_CREDENTIAL = 78

COMMITTED, UNCHANGED, STALE, INVALID, NOT_NEWER = "committed", "unchanged", "stale", "invalid", "not_newer"


class Status(object):
    def __init__(self, valid, reason="", fingerprint="", expires_at=None, raw=b""):
        self.valid = valid
        self.reason = reason
        self.fingerprint = fingerprint
        self.expires_at = expires_at
        self.raw = raw

    def remaining(self, now=None):
        if self.expires_at is None:
            return None
        return self.expires_at - (time.time() if now is None else now)

    def needs_refresh(self, window, now=None):
        left = self.remaining(now)
        return left is None or left <= window


def _expires_seconds(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value / 1000.0 if value > 100_000_000_000 else value


def parse_credential(raw):
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return Status(False, "unparseable")
    oauth = payload.get("claudeAiOauth", payload) if isinstance(payload, dict) else None
    if not isinstance(oauth, dict):
        return Status(False, "no_oauth")
    access, refresh = oauth.get("accessToken"), oauth.get("refreshToken")
    if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
        return Status(False, "incomplete")
    expires_at = _expires_seconds(oauth.get("expiresAt"))
    digest = hashlib.sha256(
        ("%s\0%s\0%s" % (access, refresh, oauth.get("expiresAt"))).encode("utf-8")
    ).hexdigest()[:32]
    return Status(True, "", digest, expires_at, raw)


def read_status(path, retries=3):
    """Lock-free read; a torn/partial write is retried briefly."""
    status = Status(False, "missing")
    for attempt in range(max(1, retries)):
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return Status(False, "missing")
        except OSError:
            raw = b""
        status = parse_credential(raw)
        if status.valid:
            return status
        if attempt + 1 < retries:
            time.sleep(0.05)
    return status


def atomic_write(dest, data):
    directory = os.path.dirname(os.path.abspath(dest))
    fd, temporary = tempfile.mkstemp(prefix=".credentials.json.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, dest)
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def exclusive_lock(lock_path, wait_sec):
    """Yield True when the lock was taken within wait_sec, else False."""
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    taken = False
    try:
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)
        deadline = time.monotonic() + max(0.0, wait_sec)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                taken = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield taken
    finally:
        if taken:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def commit(cred_path, candidate_raw, base_fingerprint, now=None):
    """Fenced replace of the authoritative file.  The caller holds the lock."""
    now = time.time() if now is None else now
    candidate = parse_credential(candidate_raw)
    if not candidate.valid:
        return INVALID, ""
    current = read_status(cred_path)
    if not current.valid or current.fingerprint != base_fingerprint:
        return STALE, ""
    if candidate.fingerprint == current.fingerprint:
        return UNCHANGED, current.fingerprint
    if (
        candidate.expires_at is None
        or candidate.expires_at <= now
        or (current.expires_at is not None and candidate.expires_at <= current.expires_at)
    ):
        return NOT_NEWER, ""
    atomic_write(cred_path, candidate_raw)
    return COMMITTED, candidate.fingerprint


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def diag(message):
    sys.stderr.write("[claude-slot-credential] %s\n" % message)
    sys.stderr.flush()


TAG_ENV = "AADS_CLAUDE_CALL_ID"
MIN_TAG_LEN = 16
SIGNAL_GRACE_SEC = float(os.environ.get("CLAUDE_SLOT_SIGNAL_GRACE_SEC", "10"))
_REFRESH_FIELDS = ("refreshToken", "refreshTokenExpiresAt")


def strip_refresh(raw):
    """Credential copy without the refresh grant, or None when unusable."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    oauth = payload.get("claudeAiOauth", payload) if isinstance(payload, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    for name in _REFRESH_FIELDS:
        oauth.pop(name, None)
    return json.dumps(payload).encode("utf-8")


def _terminate(signum, _frame):
    raise SystemExit(128 + signum)


def install_signal_handlers():
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signal.signal(getattr(signal, name), _terminate)


class SignalGuard(object):
    """Defer TERM/INT/HUP while a consumed refresh grant is not yet committed.

    A signal that arrives meanwhile is remembered and re-raised after the block, so the
    new tokens are saved first.  The block itself is bounded (refresh timeout and
    SIGNAL_GRACE_SEC after the first signal); SIGKILL cannot be deferred.
    """

    def __init__(self):
        self.pending = None
        self.since = 0.0
        self._previous = {}

    def _note(self, signum, _frame):
        if self.pending is None:
            self.pending, self.since = signum, time.monotonic()

    def expired(self):
        return self.pending is not None and time.monotonic() - self.since >= SIGNAL_GRACE_SEC

    def __enter__(self):
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            number = getattr(signal, name)
            self._previous[number] = signal.signal(number, self._note)
        return self

    def __exit__(self, exc_type, _exc, _tb):
        for number, handler in self._previous.items():
            signal.signal(number, handler)
        if self.pending is not None and exc_type is None:
            raise SystemExit(128 + self.pending)
        return False


# ── per-call process reaping ───────────────────────────────────────────────

def new_call_id():
    return uuid.uuid4().hex


def _proc_table():
    table = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as handle:
                stat = handle.read()
        except OSError:
            continue
        rest = stat[stat.rfind(b")") + 2:].split()
        if len(rest) < 20:
            continue
        table[int(name)] = (int(rest[1]), rest[0].decode("ascii", "replace"), int(rest[19]))
    return table


def _tagged_pids(table, tag):
    needle = ("%s=%s" % (TAG_ENV, tag)).encode("utf-8")
    found = set()
    for pid in table:
        try:
            with open("/proc/%d/environ" % pid, "rb") as handle:
                environ = handle.read()
        except OSError:
            continue
        if needle in environ.split(b"\0"):
            found.add(pid)
    return found


def _call_tree(tag, owned=None):
    """Live (non-zombie) pids of the tagged processes and all their descendants."""
    table = _proc_table()
    protected = set()
    pid = os.getpid()
    while pid in table and pid not in protected:
        protected.add(pid)
        pid = table[pid][0]
    members = set(_tagged_pids(table, tag))
    members.update(pid for pid, started in (owned or {}).items()
                   if pid in table and table[pid][2] == started)
    children = {}
    for pid, (ppid, _state, _started) in table.items():
        children.setdefault(ppid, []).append(pid)
    queue = list(members)
    while queue:
        for child in children.get(queue.pop(), ()):
            if child not in members:
                members.add(child)
                queue.append(child)
    return {pid for pid in members if pid not in protected and table[pid][1] != "Z"}


def _signal_all(owned, signum):
    # A pidfd pins identity across exit/reuse. Verify starttime AFTER opening it.
    # Unsupported kernels fail closed rather than risking an unrelated process.
    for pid, started in owned.items():
        fd = None
        try:
            fd = os.pidfd_open(pid)
            current = _proc_table().get(pid)
            if current and current[2] == started and current[1] != "Z":
                signal.pidfd_send_signal(fd, signum)
        except ProcessLookupError:
            pass
        except (OSError, AttributeError) as exc:
            diag("reap signal failed: %s" % exc.__class__.__name__)
        finally:
            if fd is not None:
                os.close(fd)


def reap_tag(tag, grace=5.0, kill_wait=3.0):
    """Retain PID/starttime ownership after reparenting; bounded TERM/KILL."""
    if not tag or len(tag) < MIN_TAG_LEN:
        diag("reap refused: call id missing or too short")
        return -1
    owned = {}

    def discover():
        table = _proc_table()
        for pid in _call_tree(tag, owned):
            if pid in table:
                owned.setdefault(pid, table[pid][2])
        table = _proc_table()
        return {pid: started for pid, started in owned.items()
                if pid in table and table[pid][2] == started and table[pid][1] != "Z"}

    for signum, duration in ((signal.SIGTERM, grace), (signal.SIGKILL, kill_wait)):
        deadline = time.monotonic() + min(30.0, max(0.0, duration))
        while True:
            live = discover()
            if not live:
                return 0
            _signal_all(live, signum)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return len(discover())


def reap_container(container, tag, grace=5.0, kill_wait=3.0, timeout=30.0):
    """Run the reap inside the container by piping this very file into its python."""
    try:
        with open(os.path.abspath(__file__), "rb") as source:
            result = subprocess.run(
                ["docker", "exec", "-i", container, "python3", "-", "reap",
                 "--call-id", tag, "--grace", str(grace), "--kill-wait", str(kill_wait)],
                stdin=source, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout, close_fds=True,
            )
    except (subprocess.SubprocessError, OSError, NameError) as exc:
        diag("container reap failed: %s" % exc.__class__.__name__)
        return False
    return result.returncode == 0


def cmd_reap(args):
    left = 0
    ok = True
    if args.container:
        ok = reap_container(args.container, args.call_id, args.grace, args.kill_wait, args.docker_timeout)
    left = reap_tag(args.call_id, args.grace, args.kill_wait)
    if left:
        diag("reap incomplete: %d process(es) of the call remain" % max(left, 0))
    return 0 if (left == 0 and ok) else 1


# ── refresh runners ────────────────────────────────────────────────────────

def _run_group(argv, env, timeout, guard=None):
    process = subprocess.Popen(
        argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                process.wait(timeout=0.2)
                return
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() >= deadline or (guard is not None and guard.expired()):
                raise subprocess.TimeoutExpired(argv, timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise


def _clean_env(env, home, tag):
    env = {name: value for name, value in env.items() if not name.endswith("_API_KEY")}
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN_2"):
        env.pop(name, None)
    env["HOME"] = home
    env[TAG_ENV] = tag
    return env


def refresh_host(args, seed_raw, guard=None):
    binary = args.refresh_bin or shutil.which("claude") or ""
    if not binary:
        diag("refresh skipped: no claude binary resolvable")
        return None
    home = tempfile.mkdtemp(prefix="claude-slot-refresh-")
    tag = new_call_id()
    try:
        os.chmod(home, 0o700)
        claude_dir = os.path.join(home, ".claude")
        os.makedirs(claude_dir, mode=0o700)
        target = os.path.join(claude_dir, ".credentials.json")
        atomic_write(target, seed_raw)
        settings = os.path.join(os.path.dirname(os.path.abspath(args.cred)), "settings.json")
        if os.path.isfile(settings):
            shutil.copyfile(settings, os.path.join(claude_dir, "settings.json"))
        argv = [binary] + args.refresh_args
        try:
            _run_group(argv, _clean_env(os.environ, home, tag), args.refresh_timeout, guard)
        except subprocess.TimeoutExpired:
            diag("refresh command timed out after %ss" % args.refresh_timeout)
        except OSError as exc:
            diag("refresh command could not start: %s" % exc.__class__.__name__)
        try:
            with open(target, "rb") as handle:
                return handle.read()
        except OSError:
            return None
    finally:
        if reap_tag(tag, grace=2.0, kill_wait=2.0):
            args.cleanup_failed = True
            diag("refresh cleanup incomplete")
        shutil.rmtree(home, ignore_errors=True)


def _docker(args_list, timeout=30):
    return subprocess.run(
        ["docker"] + args_list, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, timeout=timeout, close_fds=True,
    )


def refresh_docker(args, seed_raw, guard=None):
    home = "/tmp/.claude-relay-slot-refresh-%s-%d-%d" % (args.slot or "unknown", os.getpid(), random.randint(0, 99999))
    work = tempfile.mkdtemp(prefix="claude-slot-refresh-")
    seed = os.path.join(work, "seed.json")
    out = os.path.join(work, "out.json")
    tag = new_call_id()
    container = args.container
    try:
        os.chmod(work, 0o700)
        atomic_write(seed, seed_raw)
        _docker(["exec", container, "sh", "-lc", "umask 077; mkdir -p '%s/.claude'" % home])
        _docker(["cp", seed, "%s:%s/.claude/.credentials.json" % (container, home)])
        command = [
            "exec", "-i",
            "-e", "CLAUDE_SLOT_CREDENTIAL_MODE=1", "-e", "HOME=%s" % home,
            "-e", "CLAUDE_CODE_OAUTH_TOKEN=", "-e", "ANTHROPIC_AUTH_TOKEN=",
            "-e", "%s=%s" % (TAG_ENV, tag),
            container, "timeout", "-k", "5", str(int(max(1, args.refresh_timeout))),
            args.container_wrapper,
        ] + args.refresh_args
        client_env = dict(os.environ)
        client_env[TAG_ENV] = tag
        try:
            _run_group(["docker"] + command, client_env, args.refresh_timeout + 15, guard)
        except subprocess.TimeoutExpired:
            diag("refresh command timed out after %ss" % args.refresh_timeout)
        except OSError as exc:
            diag("refresh command could not start: %s" % exc.__class__.__name__)
        copied = _docker(["cp", "%s:%s/.claude/.credentials.json" % (container, home), out])
        if copied.returncode != 0:
            return None
        with open(out, "rb") as handle:
            return handle.read()
    except (subprocess.SubprocessError, OSError) as exc:
        diag("refresh transport failed: %s" % exc.__class__.__name__)
        return None
    finally:
        container_clean = reap_container(container, tag, grace=2.0, kill_wait=2.0, timeout=15.0)
        host_left = reap_tag(tag, grace=2.0, kill_wait=2.0)
        if not container_clean or host_left:
            args.cleanup_failed = True
            diag("refresh cleanup incomplete")
        with contextlib.suppress(Exception):
            _docker(["exec", container, "sh", "-lc", "rm -rf '%s'" % home])
        shutil.rmtree(work, ignore_errors=True)


# ── refresh ownership ──────────────────────────────────────────────────────

def refresh_owner(args, current, force=False):
    """Redeem the grant once.  The caller holds the slot lock and passes the freshly
    re-read authoritative status.  Returns the authoritative status afterwards."""
    attempt_path = args.lock + ".attempt"
    attempt = load_json(attempt_path)
    cooling = (
        not force
        and attempt.get("fp") == current.fingerprint
        and time.time() - float(attempt.get("at") or 0) < args.retry_sec
    )
    if cooling:
        return current
    save_json(attempt_path, {"fp": current.fingerprint, "at": time.time()})
    runner = refresh_docker if args.mode == "docker" else refresh_host
    with SignalGuard() as guard:
        candidate_raw = runner(args, current.raw, guard)
        if candidate_raw is not None:
            result, _ = commit(args.cred, candidate_raw, current.fingerprint)
            if result not in (COMMITTED, UNCHANGED):
                diag("refresh result not applied (%s)" % result)
        else:
            diag("refresh produced no credential")
    if getattr(args, "cleanup_failed", False):
        # Persist a consumed grant above before surfacing the cleanup failure.
        raise RuntimeError("refresh cleanup incomplete")
    return read_status(args.cred)


# ── prepare ───────────────────────────────────────────────────────────────

def _write_snapshot(args, status):
    stripped = strip_refresh(status.raw)
    if stripped is None:
        return False
    atomic_write(args.snapshot, stripped)
    left = status.remaining()
    save_json(args.state, {
        "pushed_fp": status.fingerprint,
        "expires_at": status.expires_at,
        "watch": bool(left is None or left <= args.watch_below),
    })
    return True


def _credential_failure(slot, status):
    if status.reason == "missing":
        sys.stderr.write("slot credential file is missing (slot=%s)\n" % slot)
    else:
        sys.stderr.write("slot credential file is incomplete (slot=%s)\n" % slot)
    return EX_CREDENTIAL


def cmd_prepare(args):
    install_signal_handlers()
    first = read_status(args.cred)
    if not first.valid:
        return _credential_failure(args.slot, first)
    if not args.force and not first.needs_refresh(args.window):
        return 0 if _write_snapshot(args, first) else _credential_failure(args.slot, first)

    with exclusive_lock(args.lock, args.lock_wait) as taken:
        if not taken:
            sys.stderr.write(
                "oauth_slot_busy: slot%s credential renewal in progress (waited %ss)\n"
                % (args.slot, args.lock_wait)
            )
            return EX_BUSY
        current = read_status(args.cred)
        if not current.valid:
            return _credential_failure(args.slot, current)
        renewed_by_other = current.fingerprint != first.fingerprint
        if current.needs_refresh(args.window) or (args.force and not renewed_by_other):
            current = refresh_owner(args, current, force=args.force)
            if not current.valid:
                return _credential_failure(args.slot, current)
            left = current.remaining()
            if left is not None and left <= 0:
                sys.stderr.write(
                    "oauth_slot_unavailable: slot%s credential renewal did not complete\n"
                    % args.slot
                )
                return EX_REFRESH_FAILED
        if not _write_snapshot(args, current):
            return _credential_failure(args.slot, current)
    return 0


# ── watch ─────────────────────────────────────────────────────────────────

def push_snapshot(args, status):
    """Replace the call's private credential atomically (CLI re-reads on mtime change)."""
    stripped = strip_refresh(status.raw)
    if stripped is None:
        return False
    if args.mode != "docker":
        atomic_write(args.push_path, stripped)
        return True
    work = tempfile.mkdtemp(prefix="claude-slot-push-")
    try:
        os.chmod(work, 0o700)
        source = os.path.join(work, "push.json")
        atomic_write(source, stripped)
        staging = args.push_path + ".new"
        if _docker(["cp", source, "%s:%s" % (args.container, staging)], timeout=20).returncode != 0:
            return False
        moved = _docker(["exec", args.container, "mv", "-f", staging, args.push_path], timeout=20)
        return moved.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def watch_tick(args, state):
    current = read_status(args.cred)
    if not current.valid:
        return
    if current.needs_refresh(args.window):
        with exclusive_lock(args.lock, args.lock_wait) as taken:
            if taken:
                fresh = read_status(args.cred)
                if fresh.valid and fresh.needs_refresh(args.window):
                    fresh = refresh_owner(args, fresh)
                if fresh.valid:
                    current = fresh
    known = state.get("expires_at") or 0
    if (
        current.fingerprint != state.get("pushed_fp")
        and current.expires_at is not None
        and current.expires_at > known
        and push_snapshot(args, current)
    ):
        state["pushed_fp"], state["expires_at"] = current.fingerprint, current.expires_at
        save_json(args.state, state)


def cmd_watch(args):
    install_signal_handlers()
    state = load_json(args.state)
    if not state.get("watch"):
        return 0
    parent = os.getppid()
    while True:
        time.sleep(args.interval)
        if os.getppid() != parent:  # the wrapper is gone; never outlive it
            return 0
        try:
            watch_tick(args, state)
        except Exception as exc:  # a watcher failure must never affect the call
            diag("watch tick failed: %s" % exc.__class__.__name__)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def owner(p, lock_wait):
        p.add_argument("--cred", required=True)
        p.add_argument("--lock", required=True)
        p.add_argument("--state", required=True)
        p.add_argument("--slot", default="unknown")
        p.add_argument("--mode", choices=("host", "docker"), default="host")
        p.add_argument("--window", type=float, default=600.0)
        p.add_argument("--lock-wait", type=float, default=lock_wait)
        p.add_argument("--refresh-timeout", type=float, default=40.0)
        p.add_argument("--retry-sec", type=float, default=60.0)
        p.add_argument("--refresh-bin", default="")
        p.add_argument("--container", default="")
        p.add_argument("--container-wrapper", default="/app/scripts/claude-oauth-wrapper.sh")
        p.add_argument("--refresh-arg", dest="refresh_args", action="append", default=[])

    prep = sub.add_parser("prepare")
    owner(prep, 25.0)
    prep.add_argument("--snapshot", required=True)
    prep.add_argument("--watch-below", type=float, default=6000.0)
    prep.add_argument("--force", action="store_true")
    prep.set_defaults(func=cmd_prepare)

    watch = sub.add_parser("watch")
    owner(watch, 3.0)
    watch.add_argument("--push-path", required=True)
    watch.add_argument("--interval", type=float, default=5.0)
    watch.set_defaults(func=cmd_watch)

    reap = sub.add_parser("reap")
    reap.add_argument("--call-id", required=True)
    reap.add_argument("--container", default="")
    reap.add_argument("--grace", type=float, default=5.0)
    reap.add_argument("--kill-wait", type=float, default=3.0)
    reap.add_argument("--docker-timeout", type=float, default=30.0)
    reap.set_defaults(func=cmd_reap)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if hasattr(args, "refresh_args") and args.refresh_args == []:
        args.refresh_args = ["-p", "ping", "--output-format", "json"]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
