#!/usr/bin/env python3
"""Gated, AADS-only Claude CLI patch updater.

The timer is intentionally conservative: a failed gate leaves the current
chat route and runner process untouched. Exit 3 means safely deferred.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit_claude_cli_runtime import (
    RELEASES, ROOT, STATE_DIR, active_container, pinned_artifact, run_version,
    runner_binary,
)

LOCK = Path("/tmp/aads-claude-cli-auto-update.lock")
DATA = Path("/root/aads/vendor/claude-cli/auto-update")
WORKTREES = Path("/root/aads/.worktrees")
MODEL = "claude-opus-5-5"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")


class Deferred(RuntimeError):
    """A safe gate did not pass; retry at the next timer tick."""


def command(argv: list[str], *, cwd: Path | None = None, timeout: int = 120,
            env: dict[str, str] | None = None) -> str:
    result = subprocess.run(argv, cwd=cwd, env=env, text=True,
                            capture_output=True, timeout=timeout)
    if result.returncode:
        # Do not log subprocess output: a CLI response can contain private data.
        raise Deferred(f"command failed ({result.returncode}): {argv[0]}")
    return result.stdout.strip()


def official_release() -> tuple[str, str, int]:
    with urlopen(f"{RELEASES}/latest", timeout=15) as response:
        version = response.read(64).decode().strip()
    if not VERSION.fullmatch(version):
        raise Deferred("official latest version is malformed")
    with urlopen(f"{RELEASES}/{version}/manifest.json", timeout=15) as response:
        manifest = json.load(response)
    platform = manifest.get("platforms", {}).get("linux-x64", {})
    checksum, size = platform.get("checksum"), platform.get("size")
    if (manifest.get("version") != version or platform.get("binary") != "claude"
            or not isinstance(checksum, str) or not HEX64.fullmatch(checksum)
            or not isinstance(size, int) or not 50_000_000 < size < 500_000_000):
        raise Deferred("official manifest identity/size/checksum failed")
    return version, checksum, size


def same_patch_lane(current: str, target: str) -> bool:
    old, new = tuple(map(int, current.split("."))), tuple(map(int, target.split(".")))
    return old[:2] == new[:2] and new >= old


def verified_binary(version: str, checksum: str, size: int) -> Path:
    target = DATA / "versions" / version / "claude"
    if target.exists():
        with target.open("rb") as existing:
            matches = target.stat().st_size == size and hashlib.file_digest(existing, "sha256").hexdigest() == checksum
        if matches:
            return target
        raise Deferred("existing staged binary has wrong size or checksum")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="claude.", dir=target.parent)
    try:
        digest = hashlib.sha256()
        count = 0
        with os.fdopen(fd, "wb") as output, urlopen(
            f"{RELEASES}/{version}/linux-x64/claude", timeout=90
        ) as response:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > size:
                    raise Deferred("official artifact exceeded manifest size")
                digest.update(chunk)
                output.write(chunk)
        if count != size or digest.hexdigest() != checksum:
            raise Deferred("official artifact failed size or SHA256 verification")
        os.chmod(temporary, 0o555)
        if run_version([temporary, "--version"]) != version:
            raise Deferred("official binary reports an unexpected version")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def replace_pin(source: str, old: str, old_sha: str, new: str, new_sha: str) -> str:
    old_url = f"{RELEASES}/{old}/linux-x64/claude"
    new_url = f"{RELEASES}/{new}/linux-x64/claude"
    changes = [
        (f"--checksum=sha256:{old_sha}", f"--checksum=sha256:{new_sha}"),
        (old_url, new_url),
        (f"= '{old} (Claude Code)'", f"= '{new} (Claude Code)'"),
        (f"Pin the official {old} Linux x64 artifact", f"Pin the official {new} Linux x64 artifact"),
        (f"official {old} manifest", f"official {new} manifest"),
    ]
    for before, after in changes:
        if source.count(before) != 1:
            raise Deferred(f"Dockerfile pin shape changed: {before[:24]}")
        source = source.replace(before, after)
    if pinned_artifact(source) != (new, new_sha):
        raise Deferred("rewritten Dockerfile pin failed audit")
    return source.rstrip("\n") + "\n"


def state() -> dict:
    path = DATA / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(value: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "state.json"
    fd, temporary = tempfile.mkstemp(prefix="state.", dir=DATA)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, sort_keys=True)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(version: str, checksum: str, size: int) -> dict:
    prior = state()
    if prior and prior.get("phase") != "complete" and prior.get("version") != version:
        raise Deferred("previous CLI candidate has not completed certification")
    command(["git", "fetch", "origin", "main"], cwd=ROOT, timeout=90)
    base = command(["git", "rev-parse", "origin/main"], cwd=ROOT)
    source = command(["git", "show", "origin/main:Dockerfile"], cwd=ROOT)
    current, old_sha = pinned_artifact(source)
    if not same_patch_lane(current, version):
        raise Deferred(f"major/minor transition or downgrade requires review: {current} -> {version}")
    if current == version:
        if old_sha != checksum:
            raise Deferred("repository checksum differs from official manifest")
        return state()
    binary = verified_binary(version, checksum, size)
    candidate = WORKTREES / f"claude-cli-autoupdate-{version}"
    if candidate.exists():
        raise Deferred(f"candidate worktree already exists: {candidate}")
    command(["git", "worktree", "add", "--detach", str(candidate), base], cwd=ROOT)
    dockerfile = candidate / "Dockerfile"
    dockerfile.write_text(replace_pin(source, current, old_sha, version, checksum))
    py = STATE_DIR / ".venv/bin/python"
    command([str(py), "-m", "pytest", "-q", "tests/unit/test_audit_claude_cli_runtime.py",
             "tests/unit/test_claude_model_contract.py"], cwd=candidate, timeout=300)
    command(["git", "add", "Dockerfile"], cwd=candidate)
    command(["git", "commit", "-m", f"chore(chat): pin verified Claude CLI {version}"],
            cwd=candidate, timeout=120)
    sha = command(["git", "rev-parse", "HEAD"], cwd=candidate)
    remote_head = command(["git", "ls-remote", "origin", "refs/heads/main"], cwd=candidate).split()
    if not remote_head or remote_head[0] != base:
        raise Deferred("main advanced during candidate preparation; push withheld")
    command(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=candidate, timeout=120)
    value = {"version": version, "sha": sha, "worktree": str(candidate),
             "binary": str(binary), "phase": "pushed"}
    save_state(value)
    return value


def chat_converged(version: str) -> bool:
    active = active_container()
    if not active:
        return False
    standby = ({"aads-server", "aads-server-green"} - {active}).pop()
    if any(run_version(["docker", "exec", slot, "/usr/local/bin/claude-aads", "--version"]) != version
           for slot in (active, standby)):
        return False
    images = [command(["docker", "inspect", slot, "--format", "{{.Image}}"])
              for slot in (active, standby)]
    return images[0] == images[1]


def release_monitor_passed(sha: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Deferred("candidate SHA is malformed")
    active = active_container()
    if not active:
        return False
    digest = command(["docker", "inspect", active, "--format", "{{.Image}}"])
    if not HEX64.fullmatch(digest.removeprefix("sha256:")):
        raise Deferred("active image digest is malformed")
    query = (
        "SELECT EXISTS(SELECT 1 FROM deploy_runs dr "
        "JOIN deploy_phase_events pe ON pe.deploy_run_id=dr.id "
        f"WHERE dr.release_sha LIKE '{sha[:12]}%' "
        f"AND dr.image_digest='{digest}' "
        "AND pe.phase='p0p1_monitoring' AND pe.status='success')"
    )
    return command(["docker", "exec", "aads-postgres", "psql", "-U", "aads", "-d", "aads",
                    "-Atc", query]) == "t"


def model_smoke() -> None:
    credential = Path("/root/.claude-relay-slots/slot1/.claude/.credentials.json")
    if not credential.is_file():
        raise Deferred("slot1 credentials unavailable for exact-model smoke")
    env = dict(os.environ, CLAUDE_SLOT_CREDENTIALS_FILE=str(credential))
    result = subprocess.run([
        str(STATE_DIR / "scripts/claude-docker-wrapper-active.sh"),
        "--model", MODEL, "-p", "Reply with OK only", "--output-format", "json",
    ], env=env, capture_output=True, text=True, timeout=120)
    if result.returncode or "Claude Code 2.1." in result.stdout + result.stderr:
        raise Deferred("exact-model smoke failed; inspect relay/slot logs")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise Deferred("exact-model smoke returned invalid JSON") from exc
    if payload.get("is_error") or not payload.get("result"):
        raise Deferred("exact-model smoke returned an API/account error")
    model_usage = payload.get("modelUsage") or {}
    if not any(MODEL in model for model in model_usage):
        raise Deferred("exact-model smoke has no Opus 5.5 execution receipt")


def converge_runner(value: dict) -> None:
    binary = Path(value["binary"])
    if run_version([str(binary), "--version"]) != value["version"]:
        raise Deferred("staged runner binary version mismatch")
    restart = STATE_DIR / "scripts/restart_local_runner.sh"
    command(["bash", str(restart), "--dry-run"], timeout=30)
    dropin = Path("/etc/systemd/system/aads-pipeline-runner.service.d/claude-cli-path.conf")
    dropin.parent.mkdir(parents=True, exist_ok=True)
    expected = f"[Service]\nEnvironment=RUNNER_CLAUDE_CLI_BIN={binary}\n"
    if not dropin.exists() or dropin.read_text() != expected:
        fd, temporary = tempfile.mkstemp(prefix="claude-cli-path.", dir=dropin.parent)
        try:
            with os.fdopen(fd, "w") as output:
                output.write(expected)
            os.replace(temporary, dropin)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        command(["systemctl", "daemon-reload"])
    command(["bash", str(restart)], timeout=60)
    effective = runner_binary()
    if effective != str(binary) or run_version([effective, "--version"]) != value["version"]:
        raise Deferred("runner effective version failed final verification")


def apply() -> dict:
    version, checksum, size = official_release()
    current, _ = pinned_artifact((ROOT / "Dockerfile").read_text())
    print(json.dumps({"official": version, "repo_pin": current, "phase": "detected"}))
    value = prepare(version, checksum, size)
    if not value or value.get("version") != version:
        # An existing manually managed release may still be draining. Never
        # deploy unrelated main commits under the updater's identity.
        raise Deferred("no updater-owned candidate; inspect deployment queue")
    if (value.get("phase") == "complete" and chat_converged(version)
            and release_monitor_passed(value["sha"])
            and runner_binary() == value.get("binary")
            and run_version([value["binary"], "--version"]) == version):
        return value
    candidate = Path(value["worktree"])
    if not candidate.is_dir() or command(["git", "rev-parse", "HEAD"], cwd=candidate) != value["sha"]:
        raise Deferred("updater-owned release worktree is missing or changed")
    if not chat_converged(version) or not release_monitor_passed(value["sha"]):
        command(["bash", "deploy.sh", "bluegreen"], cwd=candidate, timeout=3600,
                env=dict(os.environ, AADS_DEPLOY_FOREGROUND="1"))
    if not chat_converged(version) or not release_monitor_passed(value["sha"]):
        raise Deferred("chat digest/version or five-minute P0/P1 monitor has not converged")
    model_smoke()
    value["phase"] = "chat_certified"
    save_state(value)
    converge_runner(value)
    value["phase"] = "complete"
    save_state(value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="run the gated update")
    args = parser.parse_args()
    try:
        version, checksum, size = official_release()
        if not args.apply:
            pinned, _ = pinned_artifact((ROOT / "Dockerfile").read_text())
            print(json.dumps({"official": version, "pinned": pinned, "sha256": checksum,
                              "size": size, "update_available": version != pinned}))
            return 0
        with LOCK.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Deferred("another Claude CLI update is running") from exc
            result = apply()
            print(json.dumps(result, sort_keys=True))
            return 0
    except (Deferred, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"phase": "deferred", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
