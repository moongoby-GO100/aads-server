"""러너 산출물 .runner_full_diff.patch 가 결과 커밋에 변경으로 들어가지 않는다.

2026-10-08 NTV2 저장소에서 이 파일이 b370fa33(runner-324b05fd)부터 추적됐고, 작업 폴더에서 지워도
결과 커밋 8935cc89 에 내용만 바뀐 채 다시 들어갔다(+1063/-1040). 한 번 추적되면 영구히 끼어든다.

실제 commit_job_worktree_for_approval 을 실제 git 저장소에서 실행해 base..HEAD 에 이 파일의
변경이 0건인지 본다: ① 미추적 ② 이미 추적 중 ③ 워커가 직접 커밋 ④ 기존 reset 이 먹지 않은 경우.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PATCH = ".runner_full_diff.patch"


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _fn(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start:script.index("\n}\n", start) + 3]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def need_tools():
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash/git 미설치 환경")


def _repo(tmp_path: Path, tracked_patch: bool) -> tuple[Path, str]:
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    _git(wt, "config", "user.email", "t@example.com")
    _git(wt, "config", "user.name", "Test")
    (wt / "app.py").write_text("a = 1\n", encoding="utf-8")
    if tracked_patch:
        (wt / PATCH).write_text("diff --git a/old b/old\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "seed")
    base = _git(wt, "rev-parse", "HEAD")
    # 작업 시작 시점의 main 을 남기고 detached 로 작업한다(provenance 검사 전제).
    _git(wt, "checkout", "-q", "--detach")
    _git(wt, "branch", "-f", "main", base)
    return wt, base


def _harness(tmp_path: Path) -> Path:
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
record_git_diagnostics() {{ echo diag; }}
_fail_job() {{ printf '%s\\n' "$3|$4" >> "$FAIL_FILE"; return 1; }}
{_fn(script, "mask_git_diagnostics")}
{_fn(script, "verify_worker_commit_provenance")}
{_fn(script, "commit_job_worktree_for_approval")}
"""
    f = tmp_path / "fn.sh"
    f.write_text(body, encoding="utf-8")
    return f


def _fake_git_ignoring_plain_reset(tmp_path: Path) -> Path:
    """기존 `reset -q -- <patch>` 한 줄이 먹지 않는 상황을 흉내 낸다(HEAD 지정 reset 은 통과)."""
    d = tmp_path / "bin"
    d.mkdir()
    (d / "git").write_text(
        '#!/bin/bash\ncase "$*" in *"reset -q -- .runner_full_diff.patch") exit 0 ;; esac\n'
        f'exec "{shutil.which("git")}" "$@"\n',
        encoding="utf-8",
    )
    (d / "git").chmod(0o755)
    return d


def _run(tmp_path, wt, base, path_prefix=None):
    fail = tmp_path / "fail.txt"
    env = {"PATH": f"{path_prefix}:{os.environ['PATH']}" if path_prefix else os.environ["PATH"],
           "FAIL_FILE": str(fail), "HOME": str(tmp_path)}
    proc = subprocess.run(
        ["bash", "-c", f'source "{_harness(tmp_path)}"; commit_job_worktree_for_approval runner-abc12345 s "{wt}" /m "inst" "{base}"'],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr + (fail.read_text() if fail.exists() else "")
    sha = proc.stdout.strip().splitlines()[-1]
    assert sha == _git(wt, "rev-parse", "HEAD")
    return proc, sha


def _changed(wt: Path, base: str) -> list[str]:
    return _git(wt, "diff", "--name-only", f"{base}..HEAD").splitlines()


def test_untracked_patch_is_not_in_result_commit(tmp_path, need_tools):
    wt, base = _repo(tmp_path, tracked_patch=False)
    (wt / "app.py").write_text("a = 2\n", encoding="utf-8")
    (wt / PATCH).write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
    _run(tmp_path, wt, base)
    assert _changed(wt, base) == ["app.py"]
    assert (wt / PATCH).exists()


@pytest.mark.parametrize("mode", ["modified", "deleted"])
def test_tracked_patch_has_zero_changes_in_result_commit(tmp_path, need_tools, mode):
    wt, base = _repo(tmp_path, tracked_patch=True)
    (wt / "app.py").write_text("a = 2\n", encoding="utf-8")
    if mode == "modified":
        (wt / PATCH).write_text("diff --git a/new b/new\n" * 50, encoding="utf-8")
    else:
        (wt / PATCH).unlink()
    _run(tmp_path, wt, base)
    assert _changed(wt, base) == ["app.py"]
    assert _git(wt, "ls-tree", "-r", "--name-only", "HEAD").count(PATCH) == 1


@pytest.mark.parametrize("tracked", [False, True])
def test_guard_unstages_when_plain_reset_does_not_work(tmp_path, need_tools, tracked):
    wt, base = _repo(tmp_path, tracked_patch=tracked)
    (wt / "app.py").write_text("a = 2\n", encoding="utf-8")
    (wt / PATCH).write_text("diff --git a/new b/new\n", encoding="utf-8")
    proc, _ = _run(tmp_path, wt, base, path_prefix=_fake_git_ignoring_plain_reset(tmp_path))
    assert _changed(wt, base) == ["app.py"]
    assert "RUNNER_PATCH_ARTIFACT_STAGED_GUARD" in proc.stderr


@pytest.mark.parametrize("tracked", [False, True])
def test_worker_commit_containing_patch_is_neutralized(tmp_path, need_tools, tracked):
    wt, base = _repo(tmp_path, tracked_patch=tracked)
    (wt / "app.py").write_text("a = 2\n", encoding="utf-8")
    (wt / PATCH).write_text("diff --git a/worker b/worker\n", encoding="utf-8")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "worker commit")
    assert PATCH in _changed(wt, base)
    proc, _ = _run(tmp_path, wt, base)
    assert _changed(wt, base) == ["app.py"]
    assert "RUNNER_PATCH_ARTIFACT_IN_HISTORY_GUARD" in proc.stderr


def test_patch_is_written_outside_worktree_and_scripts_stay_synced():
    script = _read("pipeline-runner.sh")
    fn = _fn(script, "_persist_full_diff_if_truncated")
    assert "worktree_patch" not in fn
    assert "patch=${log_patch}" in fn
    assert script == _read("pipeline-runner.sh.local")
