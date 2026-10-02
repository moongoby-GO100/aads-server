"""러너 셸: max 전달 · 고아 claim 회수 · 서버별 THROTTLE · dedup 상호 취소 방지.

(AADS-RUNNER-CAPACITY-SSOT-ORPHAN-RECLAIM-20261002)
"""
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SH = SCRIPTS / "pipeline-runner.sh"
SRC = SH.read_text()


def _function(name: str) -> str:
    lines = SRC.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _bash(script: str):
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def test_script_is_syntactically_valid_and_local_copy_in_sync():
    assert _bash(f"bash -n {SH}").returncode == 0
    assert SH.read_text() == (SCRIPTS / "pipeline-runner.sh.local").read_text()


# ── A ─────────────────────────────────────────────────────────

def test_work_lock_acquire_passes_max():
    assert "&max=${MAX_CONCURRENT_PER_PROJECT}" in SRC
    acquire_line = next(l for l in SRC.splitlines() if "locks/work/acquire" in l)
    assert "${work_lock_max_param}" in acquire_line


# ── C ─────────────────────────────────────────────────────────

def test_orphan_claim_reclaim_is_wired_at_startup_and_periodically():
    body = _function("reclaim_orphan_claims")
    assert "ORPHAN_CLAIM_REQUEUE" in body
    for frag in ("status='claimed'", "started_at IS NULL", "INTERVAL '2 minutes'",
                 "runner_host=NULL", "status='queued'"):
        assert frag in body
    assert "runner_host=$(sql_escape" in body
    assert _function("main").count("reclaim_orphan_claims") == 2


def test_orphan_reclaim_excludes_live_jobs_and_logs_requeue():
    script = f"""
set -eo pipefail
declare -A _bg_jobs
RUNNER_HOST_NAME=host-a
RUNNER_ENGINE_MODE=general
_current_job_id=""
sql_escape() {{ echo "'$1'"; }}
record_runner_event() {{ echo "EVENT $1 $2" >&2; }}
log() {{ echo "LOG $*" >&2; }}
db_exec() {{ echo "$1"; printf 'runner-dead1\\nrunner-dead2\\n'; }}
sleep 30 & live=$!
_bg_jobs[$live]="runner-live|sess"
{_function("reclaim_orphan_claims")}
reclaim_orphan_claims
kill $live
"""
    r = _bash(script)
    assert r.returncode == 0, r.stderr
    assert "ORPHAN_CLAIM_REQUEUE job=runner-dead1" in r.stderr
    assert "ORPHAN_CLAIM_REQUEUE job=runner-dead2" in r.stderr


def test_orphan_reclaim_sql_excludes_live_job(tmp_path):
    sql_file = tmp_path / "sql.txt"
    script = f"""
declare -A _bg_jobs
RUNNER_HOST_NAME=host-a
RUNNER_ENGINE_MODE=general
_current_job_id=""
sql_escape() {{ echo "'$1'"; }}
record_runner_event() {{ :; }}
log() {{ :; }}
db_exec() {{ echo "$1" > {sql_file}; }}
sleep 30 & live=$!
_bg_jobs[$live]="runner-live|sess"
{_function("reclaim_orphan_claims")}
reclaim_orphan_claims
kill $live
"""
    r = _bash(script)
    sql = sql_file.read_text()
    assert "job_id NOT IN ('runner-live')" in sql
    assert "runner_host='host-a'" in sql


# ── D ─────────────────────────────────────────────────────────

def test_throttle_counts_only_own_host_by_default():
    main = _function("main")
    assert "RUNNER_THROTTLE_SCOPE" in main
    assert 'runner_host=$(sql_escape "$RUNNER_HOST_NAME")' in main
    count_line = next(
        l for l in main.splitlines()
        if "SELECT count(*) FROM pipeline_jobs WHERE status IN ('running','claimed')" in l
    )
    assert "${_throttle_host_filter}" in count_line


# ── E ─────────────────────────────────────────────────────────

def _run_check_duplicate(dup_job, dup_status, dup_phase):
    script = f"""
set -eo pipefail
log() {{ echo "LOG $*"; }}
check_project_lock() {{ return 0; }}
compute_instruction_hash() {{ echo hash1; }}
record_runner_event() {{ :; }}
db_update() {{ echo "UPDATE $1"; }}
db_exec() {{
  case "$1" in
    *"SELECT d.job_id"*) echo "{dup_job}" ;;
    *"SELECT status"*) echo "{dup_status}" ;;
    *"SELECT COALESCE(phase"*) echo "{dup_phase}" ;;
  esac
}}
{_function("check_duplicate")}
rc=0; check_duplicate runner-me AADS "instr" || rc=$?
echo "RC=$rc"
"""
    return _bash(script)


def test_dedup_query_ignores_cancelled_and_dedup_blocked_and_tiebreaks():
    body = _function("check_duplicate")
    assert "'cancelled'" in body
    assert "<> 'dedup_blocked'" in body
    assert "(d.created_at, d.job_id) < (me.created_at, me.job_id)" in body


def test_dedup_still_blocks_when_existing_is_really_active():
    r = _run_check_duplicate("runner-other", "running", "claude_code_work")
    assert "RC=1" in r.stdout
    assert "status='cancelled'" in r.stdout


def test_dedup_proceeds_when_existing_became_cancelled_dedup_blocked():
    r = _run_check_duplicate("runner-other", "cancelled", "dedup_blocked")
    assert "RC=0" in r.stdout
    assert "status='cancelled'" not in r.stdout
    assert "DEDUP_SKIP" in r.stdout


def test_dedup_proceeds_when_existing_row_vanished():
    r = _run_check_duplicate("runner-other", "", "")
    assert "RC=0" in r.stdout and "status='cancelled'" not in r.stdout


def test_dedup_recent_done_only_warns():
    r = _run_check_duplicate("runner-other", "done", "done")
    assert "RC=0" in r.stdout and "DEDUP_WARN" in r.stdout
    assert "status='cancelled'" not in r.stdout
