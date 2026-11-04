"""watchdog 는 자기 서버가 가져간 작업의 runner_pid 만 확인·종료한다(2026-11-04 runner-9b48ed0c)."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/pipeline-runner.sh").read_text(encoding="utf-8")


def _fn(name):
    i = SCRIPT.index(f"{name}() {{")
    return SCRIPT[i:SCRIPT.index("\n}\n", i) + 3]


def test_own_jobs_sql():
    out = subprocess.run(["bash", "-c", _fn("runner_own_jobs_sql") + "\nrunner_own_jobs_sql"],
                         env={**os.environ, "RUNNER_HOST_NAME": "contabo42"}, capture_output=True, text=True).stdout
    assert out == "AND (runner_host IS NULL OR runner_host = 'contabo42')"


def test_pid_based_queries_are_host_scoped():
    wd = _fn("_watchdog_check")
    assert "$filter $(runner_own_jobs_sql)\n                         RETURNING job_id;" in wd
    assert "WHERE status='running' AND runner_pid IS NOT NULL\n                          $filter $(runner_own_jobs_sql);" in wd
    z = SCRIPT[SCRIPT.index("zombie_rows=$(db_exec"):]
    z = z[:z.index('2>/dev/null) || true')]
    assert "$filter $(runner_own_jobs_sql);" in z
    assert (ROOT / "scripts/pipeline-runner.sh.local").read_text(encoding="utf-8") == SCRIPT
