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
    helper = script[script.index("ensure_review_hold_commit() {"):script.index(
        "# 재시도 추적 컬럼"
    )]

    assert approve < recover < promote
    assert "REVIEW_HOLD_NO_ARTIFACT" in helper
    assert "commit_hash='${current_sha}'" in helper
    assert "--no-verify" not in helper
    assert "--allow-empty" not in helper


def test_sweeper_does_not_create_new_commits_for_dirty_worktrees():
    """AADS-SWEEPER-COMMITHASH-P0: 검수받지 않은 변경이 승인 큐로 새지 않도록,
    스위퍼는 워크트리에 새 커밋을 만들지 않고 기존 HEAD 만 읽는다."""
    script = _read("review-hold-sweeper.sh")
    helper = script[script.index("ensure_review_hold_commit() {"):script.index(
        "# 재시도 추적 컬럼"
    )]

    assert 'git -C "$worktree_dir" commit -m' not in helper
    assert 'git -C "$worktree_dir" add -A' not in helper
    assert "REVIEW_HOLD_DIRTY" in helper
    assert "status --porcelain" in helper
    assert "review_feedback=COALESCE(review_feedback,'')" in helper


def test_no_hardcoded_job_id_recovery_path():
    """AADS-SWEEPER-COMMITHASH-P0 금지: 이미 NULL 로 올라간 기존 행을 특정
    job_id 하드코딩으로 임의 UPDATE 하는 경로를 다시 두지 않는다."""
    script = _read("review-hold-sweeper.sh")

    assert "repair_known_commit_gap_jobs" not in script
    assert "runner-061e59d7" not in script
    assert "runner-2136ce20" not in script
