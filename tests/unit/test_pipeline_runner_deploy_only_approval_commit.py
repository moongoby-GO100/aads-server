"""DEPLOY_ONLY job 이 승인 커밋 단계에서 "nothing to commit" 으로 죽지 않는지 고정한다.

2026-09-18 05:14 KST, runner-fd334465 / runner-5eb8be70 (GO100 배포 전용 job)
이 연속으로 approval_commit_failed 로 error 종료됐다. 원인은
commit_job_worktree_for_approval() 의 "아무 일도 하지 않은 잡" 판정
(adopted_sha == pre_exec_sha 면 채택 포기) 이 DEPLOY_ONLY job 에도 그대로
적용된 것 — DEPLOY_ONLY job 은 정의상 코드 변경/커밋을 만들지 않으므로
HEAD 가 항상 pre_exec_sha 와 같다. 선행 패치(runner-dfe127ee)는 no_changes
게이트만 우회했고 이 승인 커밋 단계는 손대지 않아 같은 구조 결함이 한 단계
뒤에서 재현됐다.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def _read_script(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


# ── 스크립트 계약 ───────────────────────────────────────────────────────


def test_local_pipeline_runner_template_matches_approval_commit_wiring():
    """두 스크립트 파일의 승인 커밋 함수가 동일해야 한다."""
    primary = _extract_function(_read_script("pipeline-runner.sh"), "commit_job_worktree_for_approval")
    local_template = _extract_function(
        _read_script("pipeline-runner.sh.local"), "commit_job_worktree_for_approval"
    )

    assert primary == local_template


def test_deploy_only_no_commit_branch_precedes_commit_attempt():
    for script_name in SCRIPTS:
        fn = _extract_function(_read_script(script_name), "commit_job_worktree_for_approval")

        assert "deploy_only_no_commit" in fn
        assert "is_deploy_only_instruction \"$instruction\"" in fn
        no_commit_idx = fn.index("DEPLOY_ONLY_APPROVAL_NO_COMMIT job=$job_id")
        commit_attempt_idx = fn.index('ALLOW_AUTH_COMMIT=1 git -C "$worktree_dir" commit')
        assert no_commit_idx < commit_attempt_idx


# ── 실행 동작 (실제 git 저장소로 함수를 직접 호출) ────────────────────────


STUBS = """
set -eo pipefail
log() { :; }
sql_escape() { printf "%s" "'$1'"; }
db_update() { :; }
_fail_job() {
    echo "FAIL_JOB reason=$3" >&2
    return 1
}
record_git_diagnostics() {
    echo "DIAG_CALLED" >&2
    echo "diag-detail"
}
verify_isolated_job_worktree() { return 0; }
record_runner_event() {
    echo "EVENT:$2" >> "$EVENT_LOG_FILE"
}
"""


@pytest.fixture(scope="module")
def commit_approval_fn(tmp_path_factory):
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash/git 미설치 환경")
    fn_file = tmp_path_factory.mktemp("commit_approval_fn") / "fn.sh"
    body = (
        STUBS
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


def test_deploy_only_no_changes_adopts_head_without_commit(tmp_path, commit_approval_fn):
    worktree_dir = tmp_path / "wt_deploy_only"
    pre_exec_sha = _init_repo(worktree_dir)
    event_log = tmp_path / "events_deploy_only.log"
    event_log.write_text("", encoding="utf-8")

    proc = _run(
        commit_approval_fn,
        event_log,
        worktree_dir,
        "DEPLOY_ONLY: true\nTASK_ID: AADS-X\nTITLE: 재배포",
        pre_exec_sha,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == pre_exec_sha
    assert "DEPLOY_ONLY_APPROVAL_NO_COMMIT" not in proc.stdout  # stdout is the sha only
    assert "EVENT:deploy_only_approval_no_commit" in event_log.read_text(encoding="utf-8")
    # HEAD 를 그대로 채택했으므로 저장소에는 새 커밋이 생기지 않는다.
    head_after = subprocess.run(
        ["git", "-C", str(worktree_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_after == pre_exec_sha


def test_non_deploy_only_no_changes_still_fails_approval_commit(tmp_path, commit_approval_fn):
    """회귀 방지: DEPLOY_ONLY 가 아니면 기존 approval_commit_failed 경로가 유지돼야 한다."""
    worktree_dir = tmp_path / "wt_regular"
    pre_exec_sha = _init_repo(worktree_dir)
    event_log = tmp_path / "events_regular.log"
    event_log.write_text("", encoding="utf-8")

    proc = _run(
        commit_approval_fn,
        event_log,
        worktree_dir,
        "TASK_ID: AADS-X\nTITLE: 일반 작업",
        pre_exec_sha,
    )

    assert proc.returncode == 1
    assert "FAIL_JOB reason=approval_commit_failed" in proc.stderr
    assert "EVENT:deploy_only_approval_no_commit" not in event_log.read_text(encoding="utf-8")


def test_deploy_only_with_real_changes_still_commits(tmp_path, commit_approval_fn):
    worktree_dir = tmp_path / "wt_deploy_only_changed"
    pre_exec_sha = _init_repo(worktree_dir)
    (worktree_dir / "new_file.txt").write_text("changed\n", encoding="utf-8")
    event_log = tmp_path / "events_changed.log"
    event_log.write_text("", encoding="utf-8")

    proc = _run(
        commit_approval_fn,
        event_log,
        worktree_dir,
        "DEPLOY_ONLY: true\nTASK_ID: AADS-X\nTITLE: 재배포",
        pre_exec_sha,
    )

    assert proc.returncode == 0, proc.stderr
    new_sha = proc.stdout.strip()
    assert new_sha != pre_exec_sha
    assert "EVENT:deploy_only_approval_no_commit" not in event_log.read_text(encoding="utf-8")
    head_after = subprocess.run(
        ["git", "-C", str(worktree_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_after == new_sha
