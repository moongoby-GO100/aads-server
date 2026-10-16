"""disk_offload_cafe24.sh: 로컬 여유공간 부족 시 덤프를 건너뛰지 않고 카페24로 스트리밍한다."""
import re
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "disk_offload_cafe24.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
[[ "${FAKE_PGDUMP_FAIL:-0}" == 1 ]] && { printf 'partial'; exit 3; }
printf 'FAKE-DUMP-PAYLOAD'
"""

# 원격을 FAKE_REMOTE_DIR 아래 로컬 디렉터리로 흉내 낸다. 마지막 인자가 원격 명령.
FAKE_SSH = """#!/usr/bin/env bash
cmd="${@: -1}"
cmd="${cmd//\\/remote\\/root/$FAKE_REMOTE_DIR}"
if [[ "${FAKE_SSH_FAIL:-0}" == 1 && "$cmd" == umask* ]]; then cat >/dev/null; exit 255; fi
if [[ "${FAKE_SSH_CORRUPT:-0}" == 1 && "$cmd" == sha256sum* ]]; then
    echo "0000000000000000000000000000000000000000000000000000000000000000  x"; echo 5; exit 0
fi
bash -c "$cmd"
"""


def _extract_function(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert match, f"{name} not found in {SCRIPT}"
    return match.group(0)


def test_choose_dump_mode_selection():
    text = SCRIPT.read_text(encoding="utf-8")
    harness = "\n".join([_extract_function(text, "choose_dump_mode"),
                         "choose_dump_mode 9863720 13190526",
                         "choose_dump_mode 13190526 13190526",
                         "choose_dump_mode 20000000 13190526",
                         "choose_dump_mode '' 13190526",
                         "choose_dump_mode abc 13190526"])
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, check=True).stdout.split()
    assert out == ["stream", "local", "local", "stream", "stream"]


def test_main_flow_streams_instead_of_skipping():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'choose_dump_mode "$free_kb" "$required_free_kb"' in text
    assert "stream_dump_to_remote" in text
    assert "DRY-RUN if free space is short: streaming path" in text
    assert "streaming to cafe24 failed" in text


def _run_stream(tmp_path: Path, **env) -> tuple[subprocess.CompletedProcess, Path]:
    text = SCRIPT.read_text(encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("ssh", FAKE_SSH)):
        f = bindir / name
        f.write_text(body, encoding="utf-8")
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    remote = tmp_path / "remote"
    remote.mkdir()
    harness = tmp_path / "h.sh"
    harness.write_text("\n".join([
        "set -uo pipefail",
        'REMOTE="fake@remote"',
        'REMOTE_ROOT="/remote/root"',
        "SSH_OPTS='-p 1'",
        'log() { printf "LOG %s\\n" "$*"; }',
        _extract_function(text, "dump_remote_path"),
        "remote_mkdir() { mkdir -p -- \"${1//\\/remote\\/root/$FAKE_REMOTE_DIR}\"; }",
        _extract_function(text, "stream_dump_to_remote"),
        'stream_dump_to_remote /local/aads_20261016.dump; rc=$?',
        'echo "RC=$rc REASON=$STREAM_FAIL_REASON"',
    ]), encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_REMOTE_DIR": str(remote), **env},
    )
    return proc, remote


def test_stream_success_uses_final_name_and_no_partial(tmp_path):
    proc, remote = _run_stream(tmp_path)
    assert "RC=0" in proc.stdout, proc.stdout + proc.stderr
    final = remote / "20261016" / "postgres" / "aads_20261016.dump"
    assert final.read_bytes() == b"FAKE-DUMP-PAYLOAD"
    assert not list(final.parent.glob("*.partial*"))


def test_stream_pg_dump_failure_cleans_partial(tmp_path):
    proc, remote = _run_stream(tmp_path, FAKE_PGDUMP_FAIL="1")
    assert "RC=1" in proc.stdout and "pg_dump=3" in proc.stdout, proc.stdout
    d = remote / "20261016" / "postgres"
    assert not (d / "aads_20261016.dump").exists()
    assert not list(d.glob("*.partial*"))


def test_stream_ssh_failure_reported(tmp_path):
    proc, remote = _run_stream(tmp_path, FAKE_SSH_FAIL="1")
    assert "RC=1" in proc.stdout and "ssh=255" in proc.stdout, proc.stdout


def test_stream_checksum_mismatch_rejected(tmp_path):
    proc, remote = _run_stream(tmp_path, FAKE_SSH_CORRUPT="1")
    assert "RC=1" in proc.stdout and "sha256 mismatch" in proc.stdout, proc.stdout
    assert not (remote / "20261016" / "postgres" / "aads_20261016.dump").exists()
