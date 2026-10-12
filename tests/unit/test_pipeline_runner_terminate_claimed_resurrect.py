"""AADS-RUNNER-TERMINATE-CLAIMED-RESURRECT-20261012.

claimed 상태에서 terminate_task 가 status='error' 를 썼는데, 워크트리 준비 뒤 도는 START UPDATE 에
상태 조건이 없어 'running' 으로 덮어써 작업이 되살아났다(runner-90cd9729, runner-3cac1d43).
START 전이는 claimed/queued 에서만 일어나고, 아니면 CLI 를 띄우지 않고 끝나야 한다.

실제 러너 함수 본문을 스크립트에서 잘라내 bash 하니스에서 돌리고, DB 는 sqlite 로 대신한다.
"""

import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")

BLOCK_START = "_abort_job_terminal_before_work() {"
BLOCK_END = "# ── 작업 실행 ─"


def _read(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _guard_block(script: str) -> str:
    start = script.index(BLOCK_START)
    end = script.index(BLOCK_END, start)
    return script[start:end]


def _run_job_body(script: str) -> str:
    start = script.index("\nrun_job() {")
    return script[start:]


def test_scripts_are_byte_identical():
    assert (ROOT / "scripts/pipeline-runner.sh").read_bytes() == (ROOT / "scripts/pipeline-runner.sh.local").read_bytes()


@pytest.mark.parametrize("name", SCRIPTS)
def test_start_update_has_status_condition(name):
    block = _guard_block(_read(name))
    m = re.search(r"UPDATE pipeline_jobs SET status='running', phase='claude_code_work'.*?;\"", block, re.S)
    assert m, "START UPDATE 를 찾지 못함"
    assert "AND status IN ('claimed','queued')" in m.group(0)
    # run_job 본문에 상태 조건 없는 START UPDATE 가 남아 있으면 안 된다.
    assert "SET status='running', phase='claude_code_work'" not in _run_job_body(_read(name))


@pytest.mark.parametrize("name", SCRIPTS)
def test_run_job_wires_guards_in_order(name):
    body = _run_job_body(_read(name))
    guard = body.index('guard_claimed_before_worktree "$job_id"')
    prep = body.index('prepare_clean_job_worktree "$job_id"')
    start = body.index('mark_job_started_or_abort "$job_id"')
    cli = body.index("MODEL_CYCLE")
    assert guard < prep < start < cli
    assert 'mark_job_started_or_abort "$job_id" "$project" "$parallel_group" || return 1' in body


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_other_unconditional_running_update(name):
    """status='running' 으로 쓰는 UPDATE 는 모두 상태 조건을 가져야 한다."""
    script = _read(name)
    for m in re.finditer(r"UPDATE pipeline_jobs SET status='running'(.*?);\"", script, re.S):
        assert re.search(r"AND status( IN \('claimed','queued'\)|='deploying')", m.group(1)), m.group(0)[:200]


HARNESS = r"""
set -u
DB="$1"; JOB="$2"; OUT="$3"
log() { echo "LOG: $*" >> "$OUT/log"; }
record_runner_event() { echo "EVENT: $2 status=$3" >> "$OUT/events"; }
_release_work_lock() { echo "RELEASE: $1 $2 $3" >> "$OUT/events"; }
_reclaim_finished_worktree() { echo "RECLAIM: $1" >> "$OUT/events"; }
db_update() {
    local sql="${1//NOW()/CURRENT_TIMESTAMP}"
    python3 - "$DB" "$sql" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute(sys.argv[2]); c.commit()
PY
}
get_job_status() {
    python3 - "$DB" "$1" <<'PY'
import sqlite3, sys
r = sqlite3.connect(sys.argv[1]).execute("SELECT status FROM pipeline_jobs WHERE job_id=?", (sys.argv[2],)).fetchone()
print(r[0] if r else "")
PY
}
BLOCK
_current_job_id="$JOB"; _current_session_id="s"
if [[ "${MODE}" == "guard" ]]; then
    guard_claimed_before_worktree "$JOB" AADS grp || exit 3
    touch "$OUT/worktree_created"
fi
mark_job_started_or_abort "$JOB" AADS grp || exit 3
touch "$OUT/cli_called"
"""


def _setup_db(path: Path, status: str):
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE pipeline_jobs (job_id TEXT PRIMARY KEY, status TEXT, phase TEXT, runner_pid INTEGER, "
        "started_at TEXT, updated_at TEXT)"
    )
    c.execute("INSERT INTO pipeline_jobs (job_id, status, phase) VALUES ('runner-t1', ?, 'claimed')", (status,))
    c.commit()
    c.close()


def _run(tmp_path: Path, name: str, mode: str, before_start_status: str | None = None, status="claimed"):
    db = tmp_path / "db.sqlite"
    out = tmp_path / "out"
    out.mkdir()
    _setup_db(db, status)
    if before_start_status:
        # terminate_task 가 claim 과 START 사이에 상태를 바꾼 상황
        c = sqlite3.connect(db)
        c.execute("UPDATE pipeline_jobs SET status=? WHERE job_id='runner-t1'", (before_start_status,))
        c.commit()
        c.close()
    script = "MODE=%s\n%s" % (mode, HARNESS.replace("BLOCK", _guard_block(_read(name))))
    proc = subprocess.run(["bash", "-c", script, "bash", str(db), "runner-t1", str(out)], capture_output=True, text=True, timeout=60)
    final = sqlite3.connect(db).execute("SELECT status, runner_pid FROM pipeline_jobs").fetchone()
    return proc, final, out


@pytest.mark.parametrize("name", SCRIPTS)
def test_terminated_claimed_job_stays_error_and_cli_not_called(tmp_path, name):
    proc, final, out = _run(tmp_path, name, "start", before_start_status="error")
    assert proc.returncode == 3, proc.stderr
    assert final[0] == "error"
    assert final[1] is None
    assert not (out / "cli_called").exists()
    log = (out / "log").read_text()
    events = (out / "events").read_text()
    assert "START_ABORTED_TERMINAL job=runner-t1 status=error" in log
    assert "EVENT: start_aborted_terminal status=error" in events
    assert "RELEASE: AADS runner-t1 grp" in events
    assert "RECLAIM: runner-t1" in events


@pytest.mark.parametrize("name", SCRIPTS)
def test_normal_claimed_job_becomes_running(tmp_path, name):
    proc, final, out = _run(tmp_path, name, "start")
    assert proc.returncode == 0, proc.stderr
    assert final[0] == "running"
    assert final[1] is not None
    assert (out / "cli_called").exists()
    assert not (out / "events").exists()


@pytest.mark.parametrize("name", SCRIPTS)
def test_queued_job_can_still_start(tmp_path, name):
    proc, final, out = _run(tmp_path, name, "start", status="queued")
    assert proc.returncode == 0, proc.stderr
    assert final[0] == "running"


@pytest.mark.parametrize("status", ["error", "cancelled", "done"])
@pytest.mark.parametrize("name", SCRIPTS)
def test_terminal_status_is_not_overwritten(tmp_path, name, status):
    proc, final, out = _run(tmp_path, name, "start", before_start_status=status)
    assert proc.returncode == 3
    assert final[0] == status
    assert not (out / "cli_called").exists()


@pytest.mark.parametrize("name", SCRIPTS)
def test_terminate_before_worktree_skips_worktree_creation(tmp_path, name):
    proc, final, out = _run(tmp_path, name, "guard", before_start_status="error")
    assert proc.returncode == 3
    assert final[0] == "error"
    assert not (out / "worktree_created").exists()
    assert not (out / "cli_called").exists()
    assert "CLAIM_LOST_BEFORE_WORKTREE job=runner-t1 status=error" in (out / "log").read_text()
    assert "EVENT: claim_lost_before_worktree status=error" in (out / "events").read_text()


@pytest.mark.parametrize("name", SCRIPTS)
def test_claimed_job_passes_worktree_guard(tmp_path, name):
    proc, final, out = _run(tmp_path, name, "guard")
    assert proc.returncode == 0, proc.stderr
    assert (out / "worktree_created").exists()
    assert (out / "cli_called").exists()
    assert final[0] == "running"
