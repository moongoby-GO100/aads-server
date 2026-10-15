"""disk_offload_cafe24.sh: 로컬 덤프는 최신 1개만 보존하고, 원격 sha256 일치 시에만 지운다."""
import os
import re
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "disk_offload_cafe24.sh"
DUMP_NAMES = ["aads_20261013.dump", "aads_20261014.dump", "aads_20261015.dump"]

FAKE_SSH = """#!/usr/bin/env bash
# 마지막 인자: sha256sum '<원격 경로>'
cmd="${@: -1}"
path="$(printf '%s' "$cmd" | sed -n "s/^sha256sum '\\(.*\\)'$/\\1/p")"
name="$(basename "$path")"
case "$FAKE_REMOTE_MODE" in
    match) sha256sum "$FAKE_DUMPS/$name" | awk -v p="$path" '{print $1 "  " p}' ;;
    mismatch) echo "0000000000000000000000000000000000000000000000000000000000000000  $path" ;;
    *) exit 1 ;;
esac
"""


def _extract_function(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match, f"{name} not found in {SCRIPT}"
    return match.group(0)


def _run_prune(tmp_path: Path, names: list[str], mode: str) -> tuple[list[str], str]:
    text = SCRIPT.read_text(encoding="utf-8")
    keep_line = re.search(r"^LOCAL_DUMP_KEEP=\d+$", text, re.M)
    assert keep_line, "LOCAL_DUMP_KEEP constant missing"

    dumps = tmp_path / "dumps"
    dumps.mkdir()
    for name in names:
        (dumps / name).write_bytes(f"fake dump {name}".encode())

    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(FAKE_SSH, encoding="utf-8")
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)

    harness = tmp_path / "harness.sh"
    harness.write_text(
        "\n".join(
            [
                "set -uo pipefail",
                keep_line.group(0),
                f'DUMPS="{dumps}"',
                'REMOTE="fake@remote"',
                'REMOTE_ROOT="/remote/root"',
                "SSH_OPTS='-p 1'",
                "ERRORS=0",
                'log() { printf "LOG %s\\n" "$*"; }',
                'fail() { log "ERROR $*"; ERRORS=$((ERRORS + 1)); }',
                _extract_function(text, "dump_remote_path"),
                _extract_function(text, "prune_verified_old_dumps"),
                "prune_verified_old_dumps",
                'echo "ERRORS=$ERRORS"',
            ]
        ),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_REMOTE_MODE": mode,
        "FAKE_DUMPS": str(dumps),
    }
    proc = subprocess.run(
        ["bash", str(harness)], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return sorted(p.name for p in dumps.iterdir()), proc.stdout


def test_keep_constant_is_one() -> None:
    assert re.search(r"^LOCAL_DUMP_KEEP=1$", SCRIPT.read_text(encoding="utf-8"), re.M)


def test_matching_remote_sha_keeps_only_latest_dump(tmp_path: Path) -> None:
    left, out = _run_prune(tmp_path, DUMP_NAMES, "match")
    assert left == ["aads_20261015.dump"]
    assert "ERRORS=0" in out


def test_mismatched_remote_sha_deletes_nothing(tmp_path: Path) -> None:
    left, out = _run_prune(tmp_path, DUMP_NAMES, "mismatch")
    assert left == sorted(DUMP_NAMES)
    assert "ERRORS=2" in out


def test_unreachable_remote_deletes_nothing(tmp_path: Path) -> None:
    left, out = _run_prune(tmp_path, DUMP_NAMES, "unreachable")
    assert left == sorted(DUMP_NAMES)
    assert "ERRORS=2" in out


def test_single_dump_is_never_deleted(tmp_path: Path) -> None:
    left, out = _run_prune(tmp_path, ["aads_20261015.dump"], "match")
    assert left == ["aads_20261015.dump"]
    assert "ERRORS=0" in out

