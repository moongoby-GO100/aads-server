"""approval_commit_stage_failed 진단성 + 재큐잉 워크트리 오염 가드 + sync 연속 defer 승격.

2026-10-02 GO100(contabo14) 러너 4건이 approval_commit_stage_failed 로 끝났다.
 1. stage 명령이 stderr 를 /dev/null 로 버려 git 의 실제 오류 문구가 남지 않았다.
 2. 재큐잉(SERVER_RESTART_REQUEUE)을 거친 job 의 actual_changed_files 에 지시서와 무관한
    파일이 섞였다.
 3. 116 의 핫픽스가 contabo14 에 닿지 않았다 — 공유 체크아웃의 scripts/pipeline-runner.sh 가
    미커밋이라 sync 타이머가 5분마다 "deferred" 만 찍고 exit 0 으로 끝났다.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _fn(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def need_tools():
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash/git 미설치 환경")


def _init_repo(repo: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    for rel, body in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return _git(repo, "rev-parse", "HEAD")


# ── 정적 계약 ──────────────────────────────────────────────────────────────


def test_both_runner_copies_are_identical():
    assert _read("pipeline-runner.sh") == _read("pipeline-runner.sh.local")


@pytest.mark.parametrize("name", SCRIPTS)
def test_stage_stderr_is_captured_not_discarded(name):
    fn = _fn(_read(name), "commit_job_worktree_for_approval")
    stage = fn[fn.index("add -A -- ."):]
    stage_line = stage.split("\n", 1)[0]
    assert ">/dev/null 2>&1" not in stage_line
    assert '2>"$stage_err"' in stage_line
    # 시크릿 마스킹 + 1KB 상한 유지
    assert "mask_git_diagnostics" in fn
    assert "head -c 1024" in fn
    assert 'record_git_diagnostics "$job_id" "approval_commit_stage_failed"' in fn


@pytest.mark.parametrize("name", SCRIPTS)
def test_scope_guard_runs_before_staging(name):
    fn = _fn(_read(name), "commit_job_worktree_for_approval")
    assert fn.index("requeue_scope_violations") < fn.index("add -A -- .")
    assert "approval_requeue_scope_violation" in fn
    # 차단·보고만 — 되돌리기/삭제 명령이 가드 함수에 없어야 한다
    guard = _fn(_read(name), "requeue_scope_violations")
    for forbidden in ("checkout", "reset", "restore", "clean", "rm ", "stash"):
        assert forbidden not in guard, forbidden


def test_sync_script_has_no_working_tree_gate_and_launcher_escalates_failures():
    """원본이 항상 origin/main export 라 작업본 비교 defer 는 없고, 승격은 fetch/export 실패에 건다."""
    s = _read("sync_pipeline_runner_remote.sh")
    assert "note_source_deferral" not in s
    assert "git -C" not in s
    launcher = _read("runner_sync_launcher.sh")
    assert "note_export_failure()" in launcher
    assert "reset_export_failure()" in launcher
    assert launcher.index("reset_export_failure\n") > launcher.index("git -C \"$SYNC_REPO\" archive")


# ── 실행 동작: stage 실패 시 git 의 stderr 가 _fail_job 에 실린다 ─────────


def _commit_fn_file(tmp_path: Path) -> Path:
    script = _read("pipeline-runner.sh")
    body = f"""
set -o pipefail
log() {{ echo "$*"; }}
sql_escape() {{ printf "%s" "'$1'"; }}
db_update() {{ :; }}
record_runner_event() {{ :; }}
verify_isolated_job_worktree() {{ return 0; }}
requeue_scope_violations() {{ return 0; }}
is_deploy_only_instruction() {{ return 1; }}
record_git_diagnostics() {{ echo "DIAG rc=$4 stderr=$6" >> "$DIAG_FILE"; echo diag; }}
_fail_job() {{ printf '%s\\n' "$3|$4" >> "$FAIL_FILE"; return 1; }}
{_fn(script, "mask_git_diagnostics")}
{_fn(script, "commit_job_worktree_for_approval")}
"""
    f = tmp_path / "commit_fn.sh"
    f.write_text(body, encoding="utf-8")
    return f


def test_stage_failure_reports_git_stderr(tmp_path, need_tools):
    wt = tmp_path / "wt"
    sha = _init_repo(wt, {"seed.txt": "seed\n"})
    (wt / "new.txt").write_text("x\n", encoding="utf-8")
    # git add 를 확실히 실패시킨다: index.lock 이 이미 있으면 "Unable to create ... index.lock"
    (wt / ".git" / "index.lock").write_text("", encoding="utf-8")
    fail_file, diag_file = tmp_path / "fail.txt", tmp_path / "diag.txt"
    proc = subprocess.run(
        ["bash", "-c", f'source "{_commit_fn_file(tmp_path)}"; commit_job_worktree_for_approval j s "{wt}" /m "inst" "{sha}"'],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "FAIL_FILE": str(fail_file), "DIAG_FILE": str(diag_file)},
    )
    assert proc.returncode == 1
    failed = fail_file.read_text(encoding="utf-8")
    assert failed.startswith("approval_commit_stage_failed|")
    assert "index.lock" in failed
    assert "rc=" in failed
    assert "index.lock" in diag_file.read_text(encoding="utf-8")


def test_stage_failure_message_is_masked_and_capped(tmp_path, need_tools):
    """git 이 토큰이 든 URL 을 오류에 실어도 _fail_job 으로는 마스킹돼 나간다."""
    wt = tmp_path / "wt"
    sha = _init_repo(wt, {"seed.txt": "seed\n"})
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git_real = shutil.which("git")
    (fake_bin / "git").write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do if [ "$a" = add ]; then\n'
        '  echo "fatal: https://oauth2:supersecrettoken@example.com/x.git sk-ant-oat01-ABCDEFGHIJKLMNOP" >&2\n'
        '  python3 -c "import sys; sys.stderr.write(\'Z\' * 5000)"\n'
        "  exit 1\n"
        "fi; done\n"
        f'exec "{git_real}" "$@"\n',
        encoding="utf-8",
    )
    (fake_bin / "git").chmod(0o755)
    fail_file, diag_file = tmp_path / "fail.txt", tmp_path / "diag.txt"
    proc = subprocess.run(
        ["bash", "-c", f'source "{_commit_fn_file(tmp_path)}"; commit_job_worktree_for_approval j s "{wt}" /m "inst" "{sha}"'],
        capture_output=True,
        text=True,
        env={"PATH": f"{fake_bin}:{os.environ['PATH']}", "FAIL_FILE": str(fail_file), "DIAG_FILE": str(diag_file)},
    )
    assert proc.returncode == 1
    failed = fail_file.read_text(encoding="utf-8")
    assert "supersecrettoken" not in failed
    assert "ABCDEFGHIJKLMNOP" not in failed
    assert "https://***@example.com" in failed
    detail = failed.split("|", 1)[1]
    assert len(detail) < 1300  # 1KB 상한 + 고정 문구


# ── 재큐잉 스코프 가드 ─────────────────────────────────────────────────────


def _guard_runner(tmp_path: Path, requeued: bool):
    script = _read("pipeline-runner.sh")
    body = f"""
db_exec() {{ echo {1 if requeued else 0}; }}
{_fn(script, "job_was_requeued")}
{_fn(script, "requeue_scope_violations")}
"""
    f = tmp_path / "guard.sh"
    f.write_text(body, encoding="utf-8")
    return f


def _violations(tmp_path, wt, instruction, pre_sha="", requeued=True, env_extra=None):
    env = {"PATH": os.environ["PATH"], **(env_extra or {})}
    return subprocess.run(
        ["bash", "-c", f'source "{_guard_runner(tmp_path, requeued)}"; requeue_scope_violations runner-abc12345 "{wt}" "$1" "$2"', "_", instruction, pre_sha],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture()
def worktree(tmp_path, need_tools):
    wt = tmp_path / "wt"
    sha = _init_repo(
        wt,
        {
            "backend/app/routers/card_trades_router.py": "a = 1\n",
            "backend/app/services/momentum/strategy_gate.py": "g = 1\n",
            "docs/HANDOVER.md": "h\n",
            "README.md": "r\n",
        },
    )
    return wt, sha


def test_requeued_job_with_unrelated_tracked_change_is_blocked_and_untouched(tmp_path, worktree):
    wt, sha = worktree
    (wt / "backend/app/services/momentum/strategy_gate.py").write_text("g = 2\n", encoding="utf-8")
    (wt / "backend/app/routers/card_trades_router.py").write_text("a = 2\n", encoding="utf-8")
    before = _git(wt, "status", "--porcelain")

    proc = _violations(tmp_path, wt, "strategy_gate.py 의 임계값을 조정하라", sha)

    assert proc.returncode == 1, proc.stderr
    assert proc.stdout.strip().splitlines() == ["backend/app/routers/card_trades_router.py"]
    # 되돌리거나 지우지 않는다
    assert _git(wt, "status", "--porcelain") == before
    assert (wt / "backend/app/routers/card_trades_router.py").read_text(encoding="utf-8") == "a = 2\n"


def test_in_scope_by_path_basename_or_directory_passes(tmp_path, worktree):
    wt, sha = worktree
    (wt / "backend/app/services/momentum/strategy_gate.py").write_text("g = 2\n", encoding="utf-8")
    (wt / "backend/app/routers/card_trades_router.py").write_text("a = 2\n", encoding="utf-8")

    by_path = _violations(tmp_path, wt, "backend/app/services/momentum/strategy_gate.py 와 backend/app/routers/card_trades_router.py 수정", sha)
    by_base = _violations(tmp_path, wt, "strategy_gate.py, card_trades_router.py", sha)
    by_dir = _violations(tmp_path, wt, "backend/app/services/momentum 와 backend/app/routers 아래 파일", sha)

    assert by_path.returncode == by_base.returncode == by_dir.returncode == 0


def test_handover_new_files_and_dot_patch_are_not_violations(tmp_path, worktree):
    wt, sha = worktree
    (wt / "docs/HANDOVER.md").write_text("h2\n", encoding="utf-8")
    (wt / "tests").mkdir()
    (wt / "tests/test_new.py").write_text("x\n", encoding="utf-8")
    _git(wt, "add", "-N", "tests/test_new.py")
    (wt / ".runner_full_diff.patch").write_text("p\n", encoding="utf-8")

    assert _violations(tmp_path, wt, "무관한 지시", sha).returncode == 0


def test_committed_unrelated_change_since_pre_exec_sha_is_caught(tmp_path, worktree):
    wt, sha = worktree
    (wt / "README.md").write_text("r2\n", encoding="utf-8")
    _git(wt, "commit", "-q", "-am", "worker commit")

    proc = _violations(tmp_path, wt, "strategy_gate.py 만", sha)

    assert proc.returncode == 1
    assert proc.stdout.strip() == "README.md"


def test_non_requeued_job_and_disabled_guard_are_not_checked(tmp_path, worktree):
    wt, sha = worktree
    (wt / "README.md").write_text("r2\n", encoding="utf-8")

    assert _violations(tmp_path, wt, "x", sha, requeued=False).returncode == 0
    assert _violations(tmp_path, wt, "x", sha, env_extra={"RUNNER_REQUEUE_SCOPE_GUARD": "0"}).returncode == 0


# ── 이전 실행 프로세스가 같은 워크트리 경로를 쓰는 중인지 ──────────────────


def test_worktree_busy_pids_finds_process_with_cwd_inside(tmp_path):
    if not Path("/proc/self/cwd").exists():
        pytest.skip("/proc 없음")
    d = tmp_path / "wt" / "sub"
    d.mkdir(parents=True)
    proc = subprocess.Popen(["sleep", "30"], cwd=d)
    try:
        time.sleep(0.2)
        f = tmp_path / "busy.sh"
        f.write_text(_fn(_read("pipeline-runner.sh"), "worktree_busy_pids"), encoding="utf-8")
        out = subprocess.run(
            ["bash", "-c", f'source "{f}"; worktree_busy_pids "{tmp_path / "wt"}"'],
            capture_output=True,
            text=True,
        ).stdout.split()
        assert str(proc.pid) in out
        empty = subprocess.run(
            ["bash", "-c", f'source "{f}"; worktree_busy_pids "{tmp_path}/nonexistent"'],
            capture_output=True,
            text=True,
        ).stdout
        assert empty == ""
    finally:
        proc.kill()
        proc.wait()


# ── sync 연속 defer 승격 ──────────────────────────────────────────────────


def test_launcher_export_failure_escalates_after_threshold(tmp_path):
    s = _read("runner_sync_launcher.sh")
    start = s.index("note_export_failure() {")
    end = s.index("EXPORT_DIR=", start)
    f = tmp_path / "fail.sh"
    f.write_text(
        'log() { echo "$*"; }\nSYNC_REPO=/repo\nSYNC_REMOTE=origin\n'
        'FAIL_STATE_FILE="$AADS_STATE"\nFAIL_ESCALATE_AFTER=3\n' + s[start:end],
        encoding="utf-8",
    )
    state = tmp_path / "state"
    env = {"PATH": os.environ["PATH"], "AADS_STATE": str(state)}

    def call(fn: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", "-c", f'source "{f}"; {fn}'], capture_output=True, text=True, env=env)

    assert "sync stalled" not in call('note_export_failure "fetch"').stdout
    assert "sync stalled" not in call('note_export_failure "fetch"').stdout
    third = call('note_export_failure "fetch"')
    assert "sync stalled" in third.stdout
    assert "3 consecutive" in third.stdout
    call("reset_export_failure")
    assert not state.exists()
    assert "sync stalled" not in call('note_export_failure "fetch"').stdout
