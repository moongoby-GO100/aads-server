from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _runner_script() -> str:
    return (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")


def test_pipeline_runner_forces_clean_origin_main_worktree():
    script = _runner_script()

    assert "prepare_clean_job_worktree()" in script
    assert "worktree add --detach \"$worktree_dir\" origin/main" in script
    assert "WORKTREE_CLEAN: $worktree_dir base=origin/main" in script
    assert "fallback to main workdir" not in script
    assert "git stash push" not in script


def test_pipeline_runner_blocks_deploy_when_main_not_clean_latest():
    script = _runner_script()

    assert "deploy_git_preflight()" in script
    assert "git_dirty_count \"$main_workdir\"" in script
    assert "git_ahead_behind_counts \"$main_workdir\" \"origin/main\"" in script
    assert "dirty=0 behind=0 ahead=0" in script
    assert "deploy_preflight_git_state" in script


def test_pipeline_runner_commits_and_gates_sha_before_approval():
    script = _runner_script()

    commit_gate = script.index("commit_job_worktree_for_approval")
    approval_transition = script.index("SET phase='awaiting_approval'")
    assert commit_gate < approval_transition
    assert "approval_commit_sha_invalid" in script
    assert "approval_commit_sha_mismatch" in script
    assert "commit_hash=$(sql_escape \"$commit_sha\")" in script


def test_pipeline_runner_pushes_only_from_verified_isolated_worktree():
    script = _runner_script()

    assert "verify_isolated_job_worktree()" in script
    assert "deploy_worktree_not_isolated" in script
    assert "deploy_commit_sha_mismatch" in script
    assert 'git -C "$worktree_dir" push origin "${current_sha}:refs/heads/main"' in script
    assert 'cd "$main_workdir"\n                echo "$diff_content" | git apply' not in script
    assert 'git -C /root/aads/aads-dashboard push' not in script
    assert 'git -C /root/webapp push' not in script
    assert "\n            git push " not in script


def test_pipeline_runner_records_masked_push_diagnostics():
    script = _runner_script()

    assert "mask_git_diagnostics()" in script
    assert "record_git_diagnostics()" in script
    for field in ("exit_code", "branch", "head_sha", "origin_url", "status", "stdout", "stderr"):
        assert f"{field}=" in script
    assert "push_fail: ${push_diag:0:1800}" in script


def test_local_pipeline_runner_template_stays_synced_with_primary_runner():
    primary = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")
    local_template = (ROOT / "scripts" / "pipeline-runner.sh.local").read_text(encoding="utf-8")

    assert local_template == primary
