"""push 전 격리 worktree dirty 검사는 러너 산출물 .runner_full_diff.patch 한 경로만 무시한다.

배경(2026-10-03): runner-3d50ee93(aads-dashboard)이 승인 직후
deploy_isolated_worktree_dirty 로 실패했다. git status 결과는 '?? .runner_full_diff.patch'
한 줄뿐이었다. 큰 diff 일 때 러너가 이 파일을 워크트리에 쓰는데 dashboard 의 .gitignore 에는
항목이 없어 dirty 로 보였다. 오류 사전: runner.full_diff_patch_dirties_dashboard_worktree
"""

import subprocess

import pytest

from tests.unit.test_pipeline_runner_deploy_preflight_ffonly_scoped import (
    _call_isolated,
    _extract_function,
    _read_script,
)

pytest_plugins = ["tests.unit.test_pipeline_runner_deploy_preflight_ffonly_scoped"]

PATCH = ".runner_full_diff.patch"


def test_dirty_gate_exclusion_is_exact_and_scripts_stay_identical():
    assert _read_script("pipeline-runner.sh") == _read_script("pipeline-runner.sh.local")
    fn = _extract_function(_read_script("pipeline-runner.sh"), "deploy_isolated_git_preflight")
    assert "grep -vxF -- '?? .runner_full_diff.patch'" in fn
    assert "deploy_isolated_worktree_dirty" in fn


def test_only_runner_patch_file_passes(isolated_case, isolated_fn_file, tmp_path):
    _remote, main, worktree, job_id, sha = isolated_case
    (worktree / PATCH).write_text("diff --git a/x b/x\n", encoding="utf-8")
    assert PATCH in subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=True,
    ).stdout
    fail_out = tmp_path / "fail.txt"
    proc = _call_isolated(isolated_fn_file, main, worktree, job_id, sha, fail_out)
    assert proc.returncode == 0, proc.stderr
    assert not fail_out.exists()


def test_other_untracked_file_still_blocks(isolated_case, isolated_fn_file, tmp_path):
    _remote, main, worktree, job_id, sha = isolated_case
    (worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    fail_out = tmp_path / "fail.txt"
    proc = _call_isolated(isolated_fn_file, main, worktree, job_id, sha, fail_out)
    assert proc.returncode == 1, proc.stderr
    assert fail_out.read_text().strip() == "deploy_isolated_worktree_dirty"


@pytest.mark.parametrize("other", ["dirty.txt", "sub/other.txt", "release.txt"])
def test_patch_plus_other_change_blocks(isolated_case, isolated_fn_file, tmp_path, other):
    _remote, main, worktree, job_id, sha = isolated_case
    (worktree / PATCH).write_text("diff --git a/x b/x\n", encoding="utf-8")
    target = worktree / other
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    fail_out = tmp_path / "fail.txt"
    proc = _call_isolated(isolated_fn_file, main, worktree, job_id, sha, fail_out)
    assert proc.returncode == 1, proc.stderr
    assert fail_out.read_text().strip() == "deploy_isolated_worktree_dirty"
