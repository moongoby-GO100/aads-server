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
    assert "ensure_approved_job_worktree()" in script
    assert 'git -C "$main_workdir" worktree add --detach "$worktree_dir" "$expected_sha"' in script
    assert "WORKTREE_RESTORED_FOR_DEPLOY" in script
    assert "deploy_worktree_not_isolated" in script
    assert "deploy_commit_sha_mismatch" in script
    assert script.index("ensure_approved_job_worktree") < script.index("deploy_commit_sha_mismatch")
    assert 'git -C "$worktree_dir" push origin "${current_sha}:refs/heads/main"' in script
    assert 'cd "$main_workdir"\n                echo "$diff_content" | git apply' not in script
    assert 'git -C /root/aads/aads-dashboard push' not in script
    assert 'git -C /root/webapp push' not in script
    assert "\n            git push " not in script


def test_pipeline_runner_does_not_deploy_dashboard_by_recent_commit_age():
    script = _runner_script()

    assert "DASHBOARD_LAST_COMMIT" not in script
    assert "DIFF_SECONDS" not in script
    assert "[ \"$DASHBOARD_CHANGED\" = true ]" in script
    assert "no dashboard-targeted changes" in script


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


def test_aads_api_release_uses_the_approved_isolated_bluegreen_path_only():
    script = _runner_script()
    aads_case = script.split("        AADS)", 1)[1].split("        KIS)", 1)[0]

    assert 'AADS_DEPLOY_SOURCE_DIR="$worktree_dir"' in aads_case
    assert 'AADS_DEPLOY_STATE_DIR="$main_workdir"' in aads_case
    assert 'bash "$worktree_dir/deploy.sh" bluegreen' in aads_case
    assert "/root/aads/aads-server/scripts/reload-api.sh" not in aads_case
    assert "bash /root/aads/aads-server/deploy.sh bluegreen" not in aads_case
    assert "diff-tree --no-commit-id --name-only -r \"$current_sha\"" in aads_case


def test_aads_rollback_uses_the_same_isolated_release_path():
    script = _runner_script()

    assert script.count('bash "$worktree_dir/deploy.sh" bluegreen') >= 2
    assert "ROLLBACK_DEPLOY: isolated bluegreen 성공" in script


def test_aads_target_routing_accepts_only_one_canonical_target_row():
    script = _runner_script()

    assert "aads_instruction_target()" in script
    assert "^[[:space:]]*TARGET[[:space:]]*:" in script
    assert "/root/aads/aads-server) target=\"backend\"" in script
    assert "/root/aads/aads-dashboard) target=\"dashboard\"" in script
    assert '"$invalid" == "true" || "$target_rows" -gt 1' in script
    assert "aads-server-other" in script
    assert "aads-server([^A-Za-z0-9_-]" not in script


def test_aads_target_routing_is_fail_closed_for_execution_deploy_and_rollback():
    script = _runner_script()

    assert "fail_invalid_aads_target()" in script
    # pre-validation, execution, approved deployment, and rejection/rollback all
    # reject the identical malformed/ambiguous TARGET decision before choosing a repo.
    assert script.count('fail_invalid_aads_target "$job_id" "$session_id"') == 4
