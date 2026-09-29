"""Fail-closed Codex account selection for the shell runner.

Only a successful read-only CLI probe may admit the JINAH account. The
receipt stores timestamps and a file mtime, never credential material.
"""
import json
import fcntl
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

_STATE_OVERRIDE = os.environ.get("AADS_CODEX_ACCOUNTS_STATE")
ACCOUNTS_ROOT = Path(os.environ.get(
    "AADS_CODEX_ACCOUNTS_ROOT",
    str(Path(_STATE_OVERRIDE).parent) if _STATE_OVERRIDE else "/root/.codex-accounts",
))
HEALTH_FILE = Path(os.environ.get("AADS_CODEX_AUTH_HEALTH_FILE", str(ACCOUNTS_ROOT / "runner-auth-health.json")))
JINAH = "CODEX_OAUTH_JINAH"
PROBE_RECEIPT = "AADS_JINAH_AUTH_PROBE_OK"


def redact_error_line(value):
    line = next((line for line in value.splitlines() if re.search(
        r"FAILED:|ERROR:|Exception|unauthorized|forbidden|invalid.?key|auth", line, re.IGNORECASE)), "")
    line = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", line)
    line = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", line)
    line = re.sub(r'(?i)(refresh_token|access_token|id_token|api_key|client_secret|authorization|cookie)(["\'\s:=]+)\S+',
                  r"\1\2[REDACTED]", line)
    line = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED]", line)
    return line[:160]


@contextmanager
def _health_lock(exclusive=False):
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if HEALTH_FILE.parent.stat().st_mode & 0o022 or HEALTH_FILE.is_symlink():
        raise PermissionError("insecure auth health path")
    lock_file = HEALTH_FILE.with_name(HEALTH_FILE.name + ".lock")
    if lock_file.is_symlink():
        raise PermissionError("insecure auth health lock")
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            raise PermissionError("insecure auth health lock")
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_health_unlocked():
    try:
        if HEALTH_FILE.is_symlink() or HEALTH_FILE.parent.stat().st_mode & 0o022:
            return {}
        value = json.loads(HEALTH_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _health():
    with _health_lock():
        return _read_health_unlocked()


def _write_health_unlocked(health):
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if HEALTH_FILE.parent.stat().st_mode & 0o022 or HEALTH_FILE.is_symlink():
        raise PermissionError("insecure auth health path")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=HEALTH_FILE.parent,
                                         prefix=".runner-auth-", delete=False) as handle:
            temp = Path(handle.name)
            os.chmod(temp, 0o600)
            json.dump(health, handle)
        os.replace(temp, HEALTH_FILE)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _write_health(health):
    with _health_lock(exclusive=True):
        _write_health_unlocked(health)


def _epoch(value):
    try:
        parsed = float(value or 0)
        return parsed if math.isfinite(parsed) else float("inf")
    except (TypeError, ValueError, OverflowError):
        return float("inf")  # malformed state fails closed for this account


def eligible(account, health, now):
    name = account.get("key_name", "")
    if not name or not name.replace("_", "").isalnum():
        return False
    if not account.get("is_active") or not account.get("has_auth"):
        return False
    try:
        limited_until = float(account.get("rate_limited_until_epoch") or 0)
        if not math.isfinite(limited_until) or limited_until > now:
            return False
    except (TypeError, ValueError):
        return False
    auth = ACCOUNTS_ROOT / name / "auth.json"
    if not auth.is_file():
        return False
    status = health.get(name, {})
    if not isinstance(status, dict) or _epoch(status.get("quarantined_until")) > now:
        return False
    if name == JINAH:
        verified = status.get("verified_until", 0)
        try:
            verified_until = float(verified)
        except (TypeError, ValueError, OverflowError):
            return False
        return (math.isfinite(verified_until) and verified_until > now
                and status.get("auth_mtime_ns") == auth.stat().st_mtime_ns)
    return True


def select():
    state_file = Path(os.environ.get("AADS_CODEX_ACCOUNTS_STATE", str(ACCOUNTS_ROOT / "state.json")))
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get("accounts"), list):
        return None
    now = time.time()
    try:
        health = _health()
    except OSError:
        return None
    candidates = [a for a in state["accounts"] if isinstance(a, dict) and eligible(a, health, now)]
    if not candidates:
        return None
    def priority(account):
        try:
            return int(account.get("priority", 9999))
        except (TypeError, ValueError):
            return 9999
    account = min(candidates, key=priority)
    return ACCOUNTS_ROOT / account["key_name"]


def quarantine(name):
    if not name or not name.replace("_", "").isalnum():
        raise ValueError("unknown account")
    state_file = Path(os.environ.get("AADS_CODEX_ACCOUNTS_STATE", str(ACCOUNTS_ROOT / "state.json")))
    try:
        accounts = json.loads(state_file.read_text()).get("accounts", [])
    except (OSError, ValueError, AttributeError):
        accounts = []
    if name not in {a.get("key_name") for a in accounts if isinstance(a, dict)}:
        raise ValueError("unknown account")
    with _health_lock(exclusive=True):
        health = _read_health_unlocked()
        health[name] = {"quarantined_until": time.time() + 86400}
        _write_health_unlocked(health)


def _record_jinah_probe(verified, auth=None):
    with _health_lock(exclusive=True):
        health = _read_health_unlocked()
        if verified:
            health[JINAH] = {
                "verified_until": time.time() + 86400,
                "auth_mtime_ns": auth.stat().st_mtime_ns,
            }
        else:
            health[JINAH] = {"quarantined_until": time.time() + 86400}
        _write_health_unlocked(health)


def probe_jinah():
    auth = ACCOUNTS_ROOT / JINAH / "auth.json"
    if not auth.is_file():
        _record_jinah_probe(False)
        return False
    env = dict(os.environ, CODEX_HOME=str(auth.parent))
    try:
        result = subprocess.run(
            ["codex", "exec", "--sandbox", "read-only", "--ephemeral",
             "-m", "gpt-5.6-luna", f"Reply only: {PROBE_RECEIPT}"],
            input="", capture_output=True, text=True, timeout=45, env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _record_jinah_probe(False)
        return False
    if result.returncode or result.stdout.strip() != PROBE_RECEIPT:
        _record_jinah_probe(False)
        return False
    _record_jinah_probe(True, auth)
    return True


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "select":
        home = select()
        if home:
            print(home)
        else:
            sys.exit(1)
    elif action == "quarantine" and len(sys.argv) == 3:
        quarantine(sys.argv[2])
    elif action == "probe-jinah":
        verified = probe_jinah()
        print("verified" if verified else "unverified")
        sys.exit(0 if verified else 1)
    elif action == "managed":
        state_file = Path(os.environ.get("AADS_CODEX_ACCOUNTS_STATE", str(ACCOUNTS_ROOT / "state.json")))
        sys.exit(0 if state_file.is_file() else 1)
    elif action == "redact-error" and len(sys.argv) == 3:
        try:
            print(redact_error_line(Path(sys.argv[2]).read_text(errors="replace")))
        except OSError:
            print()
    else:
        sys.exit(2)
