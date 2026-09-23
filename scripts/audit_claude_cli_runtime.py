#!/usr/bin/env python3
"""Read-only effective Claude CLI version audit for chat and the local runner.

The global host binary is not evidence for either execution path.  This audit
reports each path separately and fails closed on missing/unreadable evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path("/root/aads/aads-server")
RELEASES = "https://downloads.claude.ai/claude-code-releases"
VERSION_RE = re.compile(r"^([0-9]+\.[0-9]+\.[0-9]+) \(Claude Code\)$")
PIN_RE = re.compile(
    r"--checksum=sha256:([a-f0-9]{64})\s*\\?\s*"
    r"https://downloads\.claude\.ai/claude-code-releases/"
    r"([0-9]+\.[0-9]+\.[0-9]+)/linux-x64/claude",
    re.MULTILINE,
)


def pinned_artifact(dockerfile: str) -> tuple[str, str]:
    match = PIN_RE.search(dockerfile)
    if not match:
        raise ValueError("release image has no checksum-pinned Claude CLI")
    return match.group(2), match.group(1)


def run_version(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    first_line = result.stdout.strip().splitlines()
    if not first_line:
        return None
    match = VERSION_RE.fullmatch(first_line[0])
    return match.group(1) if match else None


def runner_binary() -> str:
    try:
        service_env = subprocess.run(
            ["systemctl", "show", "-p", "Environment", "--value", "aads-pipeline-runner.service"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        match = re.search(r"(?:^|\s)RUNNER_CLAUDE_CLI_BIN=(\S+)", service_env)
        if match:
            return match.group(1).strip('"\'')
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    env_file = Path("/root/.config/aads-runner.env")
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if line.startswith("RUNNER_CLAUDE_CLI_BIN="):
                return line.split("=", 1)[1].strip().strip('"\'')
    script = (STATE_DIR / "scripts/pipeline-runner.sh").read_text()
    match = re.search(r'RUNNER_CLAUDE_CLI_BIN="\$\{RUNNER_CLAUDE_CLI_BIN:-([^}]+)\}"', script)
    if not match:
        raise ValueError("runner CLI path is not inspectable")
    return match.group(1)


def active_container() -> str | None:
    try:
        name = (STATE_DIR / ".active_container").read_text().strip()
    except OSError:
        return None
    return name if name in {"aads-server", "aads-server-green"} else None


def official_latest() -> str:
    with urlopen(f"{RELEASES}/latest", timeout=10) as response:
        version = response.read(60).decode().strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("official latest did not return a version")
    return version


def audit(*, check_latest: bool = False) -> dict:
    pinned, checksum = pinned_artifact((ROOT / "Dockerfile").read_text())
    runner_path = runner_binary()
    active = active_container()
    standby = ({"aads-server", "aads-server-green"} - {active}).pop() if active else None
    result = {
        "pinned_image_version": pinned,
        "pinned_image_sha256": checksum,
        "host_global_version": run_version(["/usr/bin/claude", "--version"]),
        "runner_binary": runner_path,
        "runner_version": run_version([runner_path, "--version"]),
        "active_container": active,
        "active_chat_version": run_version(["docker", "exec", active, "/usr/local/bin/claude-aads", "--version"]) if active else None,
        "standby_container": standby,
        "standby_chat_version": run_version(["docker", "exec", standby, "/usr/local/bin/claude-aads", "--version"]) if standby else None,
    }
    if check_latest:
        result["official_latest_version"] = official_latest()
    result["converged"] = bool(
        result["runner_version"] == pinned
        and result["active_chat_version"] == pinned
        and result["standby_chat_version"] == pinned
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-latest", action="store_true")
    args = parser.parse_args()
    try:
        result = audit(check_latest=args.check_latest)
    except (OSError, ValueError, TimeoutError) as exc:
        print(json.dumps({"converged": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["converged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
