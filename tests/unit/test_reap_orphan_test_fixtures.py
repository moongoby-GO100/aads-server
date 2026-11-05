"""reap_orphan_test_fixtures.sh: 고아 pytest actor 만 정지 파일로 멈추고 나머지는 건드리지 않는다."""
import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reap_orphan_test_fixtures.sh"


def _fake_ps(tmp_path: Path, rows: str) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ps = bindir / "ps"
    ps.write_text("#!/usr/bin/env bash\ncat <<'EOF'\n" + rows + "EOF\n")
    ps.chmod(ps.stat().st_mode | stat.S_IEXEC)
    for name in ("logger", "sleep"):
        f = bindir / name
        f.write_text("#!/usr/bin/env bash\nexit 0\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
                REAP_FIXTURE_PROC_ROOT=str(tmp_path / "proc"), REAP_FIXTURE_GRACE="0")


def test_only_old_orphan_pytest_actor_is_selected(tmp_path):
    fixture = Path("/tmp") / f"pytest-of-root/pytest-999/reap-{os.getpid()}"
    fixture.mkdir(parents=True, exist_ok=True)
    try:
        proc = tmp_path / "proc" / "999991"
        proc.mkdir(parents=True)
        (proc / "environ").write_bytes(f"A=1\0FIXTURE={fixture}\0".encode())
        rows = (
            f" 999991       1   1900 bash /tmp/pytest-of-root/pytest-999/reap-{os.getpid()}/actor.sh\n"
            "  999992       1     30 bash /tmp/pytest-of-root/pytest-999/x/actor.sh\n"
            "  999993    4242   1900 bash /tmp/pytest-of-root/pytest-999/y/actor.sh\n"
            "  999994       1   9000 bash /root/scripts/pipeline-runner.sh\n"
            "  999995       1   9000 python3 /opt/go100/collector.py /tmp/pytest-of-root/pytest-1/z.sh\n"
        )
        r = subprocess.run(["bash", str(SCRIPT)], env=_fake_ps(tmp_path, rows),
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert "pids=999991" in r.stdout and "999992" not in r.stdout
        assert "999993" not in r.stdout and "999994" not in r.stdout and "999995" not in r.stdout
        assert (fixture / "stop").exists()
    finally:
        for p in (fixture / "stop",):
            p.unlink(missing_ok=True)
        fixture.rmdir()


def test_nothing_to_reap_is_silent(tmp_path):
    rows = "  1234       1   9000 bash /root/scripts/pipeline-runner.sh\n"
    r = subprocess.run(["bash", str(SCRIPT)], env=_fake_ps(tmp_path, rows),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == ""
