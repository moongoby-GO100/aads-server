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


def test_local_pipeline_runner_template_stays_synced_with_primary_runner():
    primary = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")
    local_template = (ROOT / "scripts" / "pipeline-runner.sh.local").read_text(encoding="utf-8")

    assert local_template == primary
