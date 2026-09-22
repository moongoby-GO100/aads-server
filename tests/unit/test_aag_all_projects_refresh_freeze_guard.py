from __future__ import annotations

import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/aag_all_projects_refresh.sh"


def test_entrypoint_freezes_itself_before_running():
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'if [[ -z "${AAG_FROZEN:-}" ]]; then' in source
    assert "cp -- \"$0\" \"$frozen\"" in source
    assert 'AAG_FROZEN=1 exec bash "$frozen" "$@"' in source
    # The freeze happens before flock/any real work — it must be the first
    # executable statement, not bolted on after `set -uo pipefail`.
    assert source.index("AAG_FROZEN") < source.index("exec 9>")


def test_frozen_copy_self_deletes_on_exit():
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'trap \'rm -f "$0"\' EXIT' in source


def test_freeze_guard_survives_source_file_being_overwritten_mid_run(tmp_path):
    # Regression for the 2026-09-22 11:13 CEST incident: a deploy tool
    # rewrote aag_all_projects_refresh.sh while systemd had it open mid-read,
    # and bash — which executes a running script incrementally by byte
    # offset rather than as one buffered unit — hit the shifted offset and
    # died with "line 137: syntax error near unexpected token '('".
    # Reproduce the guard in isolation (no real scan/SSH work) and prove a
    # process that started before the edit still finishes correctly.
    target = tmp_path / "guarded.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ -z "${AAG_FROZEN:-}" ]]; then\n'
        "    frozen=\"$(mktemp /tmp/aag-freeze-guard-test.XXXXXX.sh)\"\n"
        '    cp -- "$0" "$frozen"\n'
        '    chmod +x "$frozen"\n'
        '    AAG_FROZEN=1 exec bash "$frozen" "$@"\n'
        "fi\n"
        'trap \'rm -f "$0"\' EXIT\n'
        "sleep 0.5\n"
        'echo "RESULT=OK"\n',
        encoding="utf-8",
    )
    target.chmod(0o755)

    proc = subprocess.Popen(
        ["bash", str(target)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    time.sleep(0.15)
    # Simulate an external editor landing a rewrite while the frozen copy
    # (already running from its own snapshot) is mid-execution.
    target.write_text("#!/usr/bin/env bash this is not even valid bash ((( corrupted\n")

    stdout, _ = proc.communicate(timeout=10)

    assert proc.returncode == 0
    assert "RESULT=OK" in stdout

    import glob

    leftovers = glob.glob("/tmp/aag-freeze-guard-test.*.sh")
    for leftover in leftovers:
        Path(leftover).unlink(missing_ok=True)
    assert leftovers == []
