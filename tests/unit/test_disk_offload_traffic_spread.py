"""2026-11-04 CEO 지시: 카페24 전송을 트래픽 용량을 고려해 5분 간격으로 분산한다."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/disk_offload_cafe24.sh").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    start = SCRIPT.index(f"{name}() {{")
    return SCRIPT[start:SCRIPT.index("\n}\n", start) + 3]


def _config() -> str:
    start = SCRIPT.index('PHASE_GAP_SEC="${AADS_OFFLOAD_PHASE_GAP_SEC:-300}"')
    return SCRIPT[start:SCRIPT.index("\n\n", start)] if "\n\n" in SCRIPT[start:] else ""


def _run(env: dict, dry_run: int) -> subprocess.CompletedProcess:
    body = f"""
set -u
DRY_RUN={dry_run}
log() {{ echo "LOG $*"; }}
sleep() {{ echo "SLEPT $1"; }}
{_config()}
{_fn('phase_gap')}
phase_gap "new pg_dump"
echo "BW=${{RSYNC_BW[*]:-none}}"
"""
    return subprocess.run(["bash", "-c", body], env={**os.environ, **env}, capture_output=True, text=True, check=True)


def test_default_gap_is_five_minutes_and_bwlimit_applies():
    out = _run({}, 0).stdout
    assert "SLEPT 300" in out
    assert "BW=--bwlimit=10240" in out


def test_gap_and_limit_can_be_disabled():
    out = _run({"AADS_OFFLOAD_PHASE_GAP_SEC": "0", "AADS_OFFLOAD_BWLIMIT_KBPS": "0"}, 0).stdout
    assert "SLEPT" not in out
    assert "BW=none" in out


def test_dry_run_only_logs_gap():
    out = _run({}, 1).stdout
    assert "SLEPT" not in out
    assert "DRY-RUN phase gap 300s before new pg_dump" in out


def test_gaps_sit_between_transfer_phases():
    worktrees = SCRIPT.index('for wt in "$WORKTREES"/*; do')
    gap1 = SCRIPT.index('phase_gap "retained dump backfill"')
    backfill = SCRIPT.rindex("    backfill_retained_dumps\n")
    gap2 = SCRIPT.index('phase_gap "new pg_dump"')
    stream = SCRIPT.rindex('stream_dump_to_remote "$dump"')
    assert worktrees < gap1 < backfill < gap2 < stream
    assert SCRIPT.count('"${RSYNC_BW[@]}"') == 2
