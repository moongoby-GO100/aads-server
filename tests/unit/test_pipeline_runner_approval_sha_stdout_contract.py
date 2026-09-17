"""commit_job_worktree_for_approval() 의 stdout 오염으로 approval_commit_sha_mismatch
가 나며 job 이 죽는 결함을 고정한다.

2026-09-18 06:0x KST, runner-530c4ff3(커밋 52f05fa8)가 DEPLOY_ONLY 승인 커밋
우회를 정확히 넣었지만, 바로 다음 job(runner-09a6fe14, GO100 DEPLOY_ONLY)이
approval_commit_sha_mismatch 로 죽었다. 원인은 log() 가 stdout 으로도 쓰는데
(tee), commit_job_worktree_for_approval() 이 stdout 을 반환값(SHA)으로 쓰는
계약을 지키지 않아 872/875 의 정보성 log 줄이 SHA 앞에 섞여 들어간 것 —
호출부가 캡처값을 그대로 worktree HEAD 와 비교해 불일치로 판정했다.

기존 test_pipeline_runner_deploy_only_approval_commit.py 는 log() 를 `:;`
no-op 스텁으로 바꿔 두어 이 stdout 오염을 애초에 재현하지 못했다. 여기서는
실제 log()(echo | tee)를 그대로 써서 회귀를 잡는다.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")

SHA_RE = r"^[0-9a-f]{40}$"


def _read_script(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _extract_call_site_snippet(script: str) -> str:
    start = script.index('local approval_commit_sha=""')
    marker = (
        'approval_commit_sha_mismatch" "awaiting_approval 거부 — 저장 commit SHA와 '
        "runner worktree HEAD 불일치\"\n"
        '        _release_work_lock "$project" "$job_id" "$parallel_group"\n'
        "        return 1\n"
        "    fi\n"
    )
    end = script.index(marker, start) + len(marker)
    return script[start:end]


# ── 계약: 두 스크립트 파일이 동일한지 ──────────────────────────────────────


def test_scripts_stay_in_sync_for_function_and_call_site():
    fn_primary = _extract_function(_read_script("pipeline-runner.sh"), "commit_job_worktree_for_approval")
    fn_local = _extract_function(_read_script("pipeline-runner.sh.local"), "commit_job_worktree_for_approval")
    assert fn_primary == fn_local

    call_primary = _extract_call_site_snippet(_read_script("pipeline-runner.sh"))
    call_local = _extract_call_site_snippet(_read_script("pipeline-runner.sh.local"))
    assert call_primary == call_local


def test_informational_logs_inside_function_go_to_stderr():
    for script_name in SCRIPTS:
        fn = _extract_function(_read_script(script_name), "commit_job_worktree_for_approval")
        assert 'log "  DEPLOY_ONLY_APPROVAL_NO_COMMIT job=$job_id sha=$adopted_sha (DEPLOY_ONLY, 변경 없음 — 커밋 생략)" >&2' in fn
        assert 'log "  APPROVAL_COMMIT_ADOPTED_EXISTING job=$job_id sha=$adopted_sha (워커가 워크트리에서 이미 커밋)" >&2' in fn
        assert 'record_runner_event "$job_id" "deploy_only_approval_no_commit"' in fn
        assert '>/dev/null' in fn.split('record_runner_event "$job_id" "deploy_only_approval_no_commit"', 1)[1].split("\n", 1)[0]


# ── 실행 동작: 실제 log()(echo | tee)를 그대로 써서 stdout 오염을 재현/검증 ──

REAL_LOG_STUB = """
set -eo pipefail
LOG_DIR="{log_dir}"
log() {{ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_DIR/runner.log"; }}
sql_escape() {{ printf "%s" "'$1'"; }}
db_update() {{ :; }}
_fail_job() {{
    echo "FAIL_JOB reason=$3" >&2
    return 1
}}
record_git_diagnostics() {{
    echo "DIAG_CALLED" >&2
    echo "diag-detail"
}}
verify_isolated_job_worktree() {{ return 0; }}
record_runner_event() {{
    echo "LEAK_EVENT_STDOUT:$2"
    echo "EVENT:$2" >> "$EVENT_LOG_FILE"
}}
"""


@pytest.fixture()
def commit_approval_fn(tmp_path):
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash/git 미설치 환경")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    fn_file = tmp_path / "fn.sh"
    body = (
        REAL_LOG_STUB.format(log_dir=str(log_dir))
        + "\n"
        + _extract_function(_read_script(), "is_deploy_only_instruction")
        + "\n"
        + _extract_function(_read_script(), "commit_job_worktree_for_approval")
    )
    fn_file.write_text(body, encoding="utf-8")
    return fn_file


def _init_repo(repo_dir: Path) -> str:
    subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test"], check=True)
    (repo_dir / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "seed"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha


def _run(fn_file: Path, event_log: Path, worktree_dir: Path, instruction: str, pre_exec_sha: str):
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{fn_file}"; commit_job_worktree_for_approval '
            f'"job-1" "session-1" "{worktree_dir}" "/tmp/main" "$1" "$2"',
            "_",
            instruction,
            pre_exec_sha,
        ],
        capture_output=True,
        text=True,
        env={"EVENT_LOG_FILE": str(event_log), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    return proc


def test_deploy_only_no_commit_branch_stdout_is_sha_only(tmp_path, commit_approval_fn):
    worktree_dir = tmp_path / "wt_deploy_only"
    pre_exec_sha = _init_repo(worktree_dir)
    event_log = tmp_path / "events.log"
    event_log.write_text("", encoding="utf-8")

    proc = _run(
        commit_approval_fn,
        event_log,
        worktree_dir,
        "DEPLOY_ONLY: true\nTASK_ID: AADS-X\nTITLE: 재배포",
        pre_exec_sha,
    )

    assert proc.returncode == 0, proc.stderr
    # stdout 은 SHA 한 줄뿐 — 실제 log()(tee)를 썼는데도 섞이지 않아야 한다.
    assert proc.stdout.strip() == pre_exec_sha
    import re

    assert re.fullmatch(SHA_RE, proc.stdout.strip())
    assert "DEPLOY_ONLY_APPROVAL_NO_COMMIT" not in proc.stdout
    assert "LEAK_EVENT_STDOUT" not in proc.stdout  # record_runner_event 출력도 >/dev/null 로 막힌다
    assert "DEPLOY_ONLY_APPROVAL_NO_COMMIT" in proc.stderr
    assert "EVENT:deploy_only_approval_no_commit" in event_log.read_text(encoding="utf-8")


def test_adopted_existing_branch_stdout_is_sha_only(tmp_path, commit_approval_fn):
    """워커가 워크트리 안에서 이미 커밋해 둔 경우(비-DEPLOY_ONLY)도 같은 결함이 있었다."""
    worktree_dir = tmp_path / "wt_adopted"
    pre_exec_sha = _init_repo(worktree_dir)
    (worktree_dir / "worker_change.txt").write_text("already committed by worker\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree_dir), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(worktree_dir), "commit", "-q", "-m", "worker committed"], check=True)
    worker_sha = subprocess.run(
        ["git", "-C", str(worktree_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert worker_sha != pre_exec_sha

    event_log = tmp_path / "events.log"
    event_log.write_text("", encoding="utf-8")

    proc = _run(
        commit_approval_fn,
        event_log,
        worktree_dir,
        "TASK_ID: AADS-X\nTITLE: 일반 작업",
        pre_exec_sha,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == worker_sha
    import re

    assert re.fullmatch(SHA_RE, proc.stdout.strip())
    assert "APPROVAL_COMMIT_ADOPTED_EXISTING" not in proc.stdout
    assert "APPROVAL_COMMIT_ADOPTED_EXISTING" in proc.stderr


# ── 호출부 방어: 오염된 캡처값에서도 SHA 를 추출한다 ─────────────────────────


CALL_SITE_HARNESS = """
set -eo pipefail
log() {{ echo "[LOG] $*" >&2; }}
_release_work_lock() {{ :; }}
_cleanup_artifacts() {{ :; }}
promote_next_queued() {{ :; }}
_fail_job() {{
    echo "FAIL_JOB reason=$3" >&2
    return 1
}}
commit_job_worktree_for_approval() {{
    printf '%b' "$STUB_CAPTURE"
}}

call_site_snippet() {{
    local job_id="job-1" session_id="session-1" worktree_dir="$1" main_workdir="/tmp/main"
    local instruction="x" pre_exec_sha="y" parallel_group="" project="test"
{snippet}
    echo "APPROVAL_SHA_RESULT:$approval_commit_sha"
    return 0
}}

call_site_snippet "$1"
"""


@pytest.fixture()
def call_site_fn_file(tmp_path):
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash/git 미설치 환경")
    snippet = _extract_call_site_snippet(_read_script())
    # 스니펫은 4-space 들여쓰기 기준 함수 본문 그대로이므로 harness 안에 그대로 삽입한다.
    fn_file = tmp_path / "call_site.sh"
    fn_file.write_text(CALL_SITE_HARNESS.format(snippet=snippet), encoding="utf-8")
    return fn_file


def _run_call_site(fn_file: Path, worktree_dir: Path, stub_capture: str):
    return subprocess.run(
        ["bash", str(fn_file), str(worktree_dir)],
        capture_output=True,
        text=True,
        env={"STUB_CAPTURE": stub_capture, "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )


def test_call_site_extracts_sha_from_contaminated_capture(tmp_path, call_site_fn_file):
    worktree_dir = tmp_path / "wt"
    head_sha = _init_repo(worktree_dir)
    contaminated = f"[2026-09-18 06:00:00] DEPLOY_ONLY_APPROVAL_NO_COMMIT job=x sha=y (blah)\\n{head_sha}"

    proc = _run_call_site(call_site_fn_file, worktree_dir, contaminated)

    assert proc.returncode == 0, proc.stderr
    assert f"APPROVAL_SHA_RESULT:{head_sha}" in proc.stdout
    assert "APPROVAL_SHA_STDOUT_SANITIZED" in proc.stderr
    assert "FAIL_JOB" not in proc.stderr


def test_call_site_still_fails_when_no_sha_present(tmp_path, call_site_fn_file):
    worktree_dir = tmp_path / "wt2"
    _init_repo(worktree_dir)
    garbage = "[2026-09-18 06:00:00] SOME_UNRELATED_LOG_LINE_NO_SHA_HERE"

    proc = _run_call_site(call_site_fn_file, worktree_dir, garbage)

    assert proc.returncode == 1
    assert "FAIL_JOB reason=approval_commit_sha_mismatch" in proc.stderr
