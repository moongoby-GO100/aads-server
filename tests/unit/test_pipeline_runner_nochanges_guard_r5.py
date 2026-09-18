"""AADS-RUNNER-NOCHANGES-GUARD-P1-R5.

run_job() 의 no_changes 판정이 diff 0건을 보자마자 종결해서 두 가지를
놓쳤다.

(A) 타이밍 레이스 — runner-71bc9b4e: 판정 후 77초 뒤에 커밋이 origin/main 에
    나타났다. 판정이 커밋보다 먼저 일어났을 뿐인데 no_changes 로 종결됐다.
(B) 커밋 누락 — runner-528d848f/4c3126d0/e6301a3f/7049ed0c: 파일은 고쳤는데
    커밋을 안 했다. 가드는 diff 유무만 보므로 (B)를 (A)와 똑같이 "변경
    없음"으로 종결했고, 워크트리를 지우면서 패치가 그대로 소실됐다.

이 테스트는 no_changes 판정 블록에 넣은 재확인 루프(재시도 중 diff 가
생기면 정상 경로로 빠져나감)와, 재확인이 소진됐을 때 워크트리가 dirty 면
no_changes 대신 uncommitted_worktree_changes 로 error 종결하는 분기를
고정한다.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")

START_ANCHOR = 'if [[ ! "$git_diff" =~ [^[:space:]] ]]; then\n        # AADS-RUNNER-NOCHANGES-GUARD-P1-R5'
END_ANCHOR = "\n\n    # ═══ AI Reviewer 단계"


def _read_script(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_no_changes_block(script: str) -> str:
    start = script.index(START_ANCHOR)
    end = script.index(END_ANCHOR, start)
    return script[start:end]


def _extract_diff_contract_block(script: str) -> str:
    """재확인 루프가 쓰는 git_diff 캡처는 공유 블록에 있다(job_diff_contract).

    하니스에 스텁을 두지 않고 진짜 본문을 얹는다 — 캡처 규칙이 바뀌면 여기서도
    같이 깨져야 한다.
    """
    start = script.index("# ─── SHARED-BLOCK BEGIN: job_diff_contract")
    end = script.index("# ─── SHARED-BLOCK END: job_diff_contract")
    return script[start:end]


# ── 정적 계약 ────────────────────────────────────────────────────────────


def test_scripts_are_byte_identical():
    primary = (ROOT / "scripts" / "pipeline-runner.sh").read_bytes()
    local = (ROOT / "scripts" / "pipeline-runner.sh.local").read_bytes()
    assert primary == local


def test_no_changes_block_identical_across_scripts():
    a = _extract_no_changes_block(_read_script("pipeline-runner.sh"))
    b = _extract_no_changes_block(_read_script("pipeline-runner.sh.local"))
    assert a == b


def test_recheck_attempt_declared_and_incremented():
    block = _extract_no_changes_block(_read_script())
    assert "local _recheck_attempt=0" in block
    assert "_recheck_attempt=$((_recheck_attempt + 1))" in block
    # 선언이 증가보다 먼저 나와야 한다.
    assert block.index("local _recheck_attempt=0") < block.index("_recheck_attempt=$((_recheck_attempt + 1))")


def test_sigterm_sigint_trap_exists_and_exits_130():
    block = _extract_no_changes_block(_read_script())
    assert "trap '_recheck_signaled=1' TERM INT" in block
    assert "trap - TERM INT" in block
    signaled_branch = block[block.index("NO_CHANGES_RECHECK_SIGNALED") - 200: block.index("NO_CHANGES_RECHECK_SIGNALED") + 200]
    assert "return 130" in signaled_branch


def test_env_var_defaults_and_125s_formula_documented():
    block = _extract_no_changes_block(_read_script())
    assert 'NO_CHANGES_RECHECK_MAX_ATTEMPTS:-5' in block
    assert 'NO_CHANGES_RECHECK_SLEEP_SECONDS:-25' in block
    assert "5회 × 25초 = 125초" in block


def test_uncommitted_worktree_changes_branch_present():
    block = _extract_no_changes_block(_read_script())
    assert "git status --porcelain" in block
    assert "error_detail='uncommitted_worktree_changes'" in block
    assert "status='error'" in block
    # dirty 목록 최대 20개
    assert "head -20" in block
    # 워크트리를 지우지 않고 회수 가능하게 남긴다 — 이 분기에는 worktree remove 가 없어야 한다.
    dirty_branch_start = block.index("_dirty_status=$(git status --porcelain")
    dirty_branch_end = block.index("log \"  NO_CHANGES job=$job_id target=$target_repo")
    dirty_branch = block[dirty_branch_start:dirty_branch_end]
    assert "git worktree remove" not in dirty_branch
    assert "_preserve_worktree_patch" in dirty_branch


def test_recheck_exhausted_log_present_before_existing_terminal_paths():
    block = _extract_no_changes_block(_read_script())
    exhausted_idx = block.index("NO_CHANGES_RECHECK_EXHAUSTED")
    read_only_idx = block.index("NO_CHANGES_READ_ONLY job=$job_id")
    no_changes_idx = block.index('log "  NO_CHANGES job=$job_id target=$target_repo')
    assert exhausted_idx < read_only_idx < no_changes_idx


# ── 동적 실행 계약 ───────────────────────────────────────────────────────

REAL_STUBS = """
LOG_FILE="{log_file}"
DB_LOG="{db_log}"
EVENT_LOG="{event_log}"
CHAT_LOG="{chat_log}"

log() {{ printf '%s\\n' "$*" | tee -a "$LOG_FILE" >&2; }}
sql_escape() {{ printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"; }}
db_update() {{ printf '%s\\n---\\n' "$1" >> "$DB_LOG"; }}
record_runner_event() {{ printf 'EVENT:%s\\n' "$*" >> "$EVENT_LOG"; }}
post_to_chat() {{ printf 'CHAT:%s\\n' "$*" >> "$CHAT_LOG"; }}
_release_work_lock() {{ :; }}
_cleanup_artifacts() {{ :; }}
_notify_ai() {{ :; }}
promote_next_queued() {{ :; }}
record_actual_changed_files() {{ printf 'ACF:%s\\n' "$2" >> "$DB_LOG"; }}
_preserve_worktree_patch() {{ printf 'PRESERVED:%s %s\\n' "$1" "$2" >> "$DB_LOG"; }}
is_read_only_instruction() {{ return "${{STUB_READ_ONLY:-1}}"; }}
is_deploy_only_instruction() {{ return "${{STUB_DEPLOY_ONLY:-1}}"; }}
"""

HARNESS_TAIL = """
run_block() {
    local job_id="job-1" project="TESTPROJ" instruction="$INSTR" session_id="sess-1"
    local job_model="auto" job_size="M" parallel_group=""
    local target_repo="default"
    local workdir="$WORKDIR" worktree_dir="$WORKDIR" main_workdir="$WORKDIR"
    local pre_exec_sha="$PRE_SHA"
    local output="stub output" output_file="$OUTPUT_FILE"
    local git_diff=""
    local actual_changed_files=""
    cd "$workdir"

%s

    echo "FELL_THROUGH"
    return 42
}

run_block
"""


def _init_repo(repo_dir: Path) -> str:
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    (repo_dir / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "seed"], check=True)
    return subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def harness_file(tmp_path):
    block = _extract_no_changes_block(_read_script())
    log_file = tmp_path / "runner.log"
    db_log = tmp_path / "db.log"
    event_log = tmp_path / "events.log"
    chat_log = tmp_path / "chat.log"
    for f in (log_file, db_log, event_log, chat_log):
        f.write_text("", encoding="utf-8")

    fn_file = tmp_path / "harness.sh"
    fn_file.write_text(
        "set -eo pipefail\n"
        + _extract_diff_contract_block(_read_script())
        + REAL_STUBS.format(
            log_file=str(log_file), db_log=str(db_log),
            event_log=str(event_log), chat_log=str(chat_log),
        )
        + (HARNESS_TAIL % block),
        encoding="utf-8",
    )
    return {
        "fn_file": fn_file,
        "log_file": log_file,
        "db_log": db_log,
        "event_log": event_log,
        "chat_log": chat_log,
    }


def _run_harness(harness_file, env_overrides):
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        **env_overrides,
    }
    return subprocess.run(
        ["bash", str(harness_file["fn_file"])],
        capture_output=True, text=True, env=env,
    )


def test_recheck_recovers_when_commit_appears_during_wait(tmp_path, harness_file):
    """레이스 케이스 A: 첫 판정 0건 → 재확인 중 커밋 발생 → no_changes 로 종결하지 않고 정상 경로로 빠져나간다."""
    if not (__import__("shutil").which("bash") and __import__("shutil").which("git")):
        pytest.skip("bash/git 미설치 환경")

    worktree_dir = tmp_path / "wt_race"
    pre_sha = _init_repo(worktree_dir)

    import threading

    def _commit_late():
        import time
        time.sleep(0.5)
        (worktree_dir / "late.txt").write_text("committed after judgement\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree_dir), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(worktree_dir), "commit", "-q", "-m", "late commit"], check=True)

    t = threading.Thread(target=_commit_late)
    t.start()
    try:
        proc = _run_harness(
            harness_file,
            {
                "INSTR": "TASK_ID: AADS-X\nTITLE: 일반 작업",
                "WORKDIR": str(worktree_dir),
                "PRE_SHA": pre_sha,
                "OUTPUT_FILE": str(tmp_path / "out.txt"),
                "NO_CHANGES_RECHECK_MAX_ATTEMPTS": "3",
                "NO_CHANGES_RECHECK_SLEEP_SECONDS": "1",
            },
        )
    finally:
        t.join()

    assert proc.returncode == 42, proc.stderr  # 정상 경로로 블록을 빠져나가 FELL_THROUGH 까지 도달
    assert "FELL_THROUGH" in proc.stdout
    log_text = harness_file["log_file"].read_text(encoding="utf-8")
    assert "NO_CHANGES_RECHECK_RECOVERED" in log_text
    assert "NO_CHANGES_RECHECK_EXHAUSTED" not in log_text
    assert "NO_CHANGES job=" not in log_text
    db_text = harness_file["db_log"].read_text(encoding="utf-8")
    assert "status='cancelled'" not in db_text
    assert "uncommitted_worktree_changes" not in db_text


def test_recheck_exhausted_clean_worktree_stays_no_changes(harness_file, tmp_path):
    """재확인을 다 써도 diff 도 없고 워크트리도 clean 이면 기존대로 no_changes 로 종결한다."""
    if not (__import__("shutil").which("bash") and __import__("shutil").which("git")):
        pytest.skip("bash/git 미설치 환경")

    worktree_dir = tmp_path / "wt_clean"
    pre_sha = _init_repo(worktree_dir)

    proc = _run_harness(
        harness_file,
        {
            "INSTR": "TASK_ID: AADS-X\nTITLE: 일반 작업",
            "WORKDIR": str(worktree_dir),
            "PRE_SHA": pre_sha,
            "OUTPUT_FILE": str(tmp_path / "out.txt"),
            "NO_CHANGES_RECHECK_MAX_ATTEMPTS": "2",
            "NO_CHANGES_RECHECK_SLEEP_SECONDS": "1",
        },
    )

    assert proc.returncode == 1, proc.stderr
    log_text = harness_file["log_file"].read_text(encoding="utf-8")
    assert "NO_CHANGES_RECHECK_EXHAUSTED" in log_text
    assert "attempts=2" in log_text
    assert "NO_CHANGES job=" in log_text
    assert "UNCOMMITTED_WORKTREE_CHANGES" not in log_text
    db_text = harness_file["db_log"].read_text(encoding="utf-8")
    assert "status='cancelled'" in db_text
    assert "uncommitted_worktree_changes" not in db_text


def test_recheck_exhausted_dirty_worktree_errors_as_uncommitted(harness_file, tmp_path):
    """케이스 B: 재확인을 다 써도 diff 는 0건이지만 워크트리에 미커밋 변경이 있으면
    no_changes 가 아니라 uncommitted_worktree_changes 로 error 종결해야 한다."""
    if not (__import__("shutil").which("bash") and __import__("shutil").which("git")):
        pytest.skip("bash/git 미설치 환경")

    worktree_dir = tmp_path / "wt_dirty"
    pre_sha = _init_repo(worktree_dir)
    # 새 파일을 만들되 git add/commit 은 하지 않는다 — untracked 파일은 `git diff`에
    # 잡히지 않아 재확인 루프 내내 git_diff 는 계속 비어 있지만, `git status
    # --porcelain`에는 남는다. 이것이 실측 케이스 B(파일은 고쳤는데 커밋을 안 함)다.
    (worktree_dir / "dirty.txt").write_text("uncommitted change\n", encoding="utf-8")

    proc = _run_harness(
        harness_file,
        {
            "INSTR": "TASK_ID: AADS-X\nTITLE: 일반 작업",
            "WORKDIR": str(worktree_dir),
            "PRE_SHA": pre_sha,
            "OUTPUT_FILE": str(tmp_path / "out.txt"),
            "NO_CHANGES_RECHECK_MAX_ATTEMPTS": "2",
            "NO_CHANGES_RECHECK_SLEEP_SECONDS": "1",
        },
    )

    assert proc.returncode == 1, proc.stderr
    log_text = harness_file["log_file"].read_text(encoding="utf-8")
    assert "UNCOMMITTED_WORKTREE_CHANGES" in log_text
    assert "NO_CHANGES job=" not in log_text
    db_text = harness_file["db_log"].read_text(encoding="utf-8")
    assert "error_detail='uncommitted_worktree_changes'" in db_text
    assert "status='error'" in db_text
    assert "dirty.txt" in db_text
    assert f"PRESERVED:job-1 {worktree_dir}" in db_text
    chat_text = harness_file["chat_log"].read_text(encoding="utf-8")
    assert "커밋 누락" in chat_text


def test_sigterm_during_recheck_wait_exits_130_without_zombie(harness_file, tmp_path):
    """sleep 도중 SIGTERM 을 받으면 루프를 즉시 중단하고 130 으로 빠져나간다."""
    if not (__import__("shutil").which("bash") and __import__("shutil").which("git")):
        pytest.skip("bash/git 미설치 환경")

    worktree_dir = tmp_path / "wt_sigterm"
    pre_sha = _init_repo(worktree_dir)

    import signal
    import time

    proc = subprocess.Popen(
        ["bash", str(harness_file["fn_file"])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "INSTR": "TASK_ID: AADS-X\nTITLE: 일반 작업",
            "WORKDIR": str(worktree_dir),
            "PRE_SHA": pre_sha,
            "OUTPUT_FILE": str(tmp_path / "out.txt"),
            "NO_CHANGES_RECHECK_MAX_ATTEMPTS": "10",
            "NO_CHANGES_RECHECK_SLEEP_SECONDS": "5",
        },
    )
    time.sleep(0.8)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=10)

    assert "NO_CHANGES_RECHECK_SIGNALED" in stderr
    assert "FELL_THROUGH" not in stdout
    # 프로세스가 좀비로 남지 않고 정상 회수됐는지(wait 가능했는지)는 communicate() 성공으로 확인된다.
    assert proc.returncode == 130
