"""review_hold 산출물의 commit_hash 누락 회귀 방지."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_runner_commits_before_review_and_keeps_hash_on_hold():
    script = _read("pipeline-runner.sh")
    commit = script.index(
        'approval_commit_sha=$(commit_job_worktree_for_approval "$job_id"'
    )
    review = script.index('local review_verdict="APPROVE"', commit)
    hold = script.index('if [[ "$review_verdict" != "APPROVE" ]]', review)
    hold_update_end = script.index('record_runner_event "$job_id" "job_terminal"', hold)

    assert commit < review < hold
    assert "commit_hash=NULL" not in script[hold:hold_update_end].replace(" ", "")
    assert "SET commit_hash=NULL" not in script[hold:hold_update_end]


def test_runner_no_changes_exits_before_commit_without_empty_commit():
    script = _read("pipeline-runner.sh")
    no_changes = script.index('NO_CHANGES job=$job_id')
    no_changes_return = script.index("return 1", no_changes)
    commit = script.index(
        'approval_commit_sha=$(commit_job_worktree_for_approval "$job_id"'
    )

    assert no_changes < no_changes_return < commit
    assert "--allow-empty" not in script


def test_sweeper_recovers_commit_before_promotion_and_fails_closed_without_artifact():
    script = _read("review-hold-sweeper.sh")
    approve = script.index('if [[ "$verdict" == "APPROVE" ]]')
    recover = script.index('ensure_review_hold_commit "$job_id"', approve)
    promote = script.index("SET status='awaiting_approval'", recover)
    helper = script[script.index("ensure_review_hold_commit() {"):script.index("repair_known_commit_gap_jobs() {")]

    assert approve < recover < promote
    assert "REVIEW_HOLD_NO_ARTIFACT" in helper
    assert 'git -C "$worktree_dir" commit -m' in helper
    assert "--no-verify" not in helper
    assert "--allow-empty" not in helper
    assert "commit_hash='${current_sha}'" in helper


def test_known_blocked_jobs_have_non_deploying_recovery_paths():
    script = _read("review-hold-sweeper.sh")
    repair = script[script.index("repair_known_commit_gap_jobs() {"):script.index(
        'select_sql="SELECT', script.index("repair_known_commit_gap_jobs() {")
    )]

    assert "runner-061e59d7" in repair
    assert 'recovered_sha="78fef4c7"' in repair
    assert 'ensure_review_hold_commit "runner-2136ce20"' in repair
    assert "push" not in repair
    assert "deploy" not in repair
