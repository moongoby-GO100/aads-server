"""AADS-RUNNER-REJECTED-ARTIFACT-PRESERVE-20260918

runner-1f09e9e5 는 AI 리뷰가 오판으로 반려한 뒤 워크트리가 즉시 삭제되고
DB git_diff 도 50KB 상한에 잘려 있어 산출물을 복구할 수 없었다. 이 회귀
테스트는 review_failed(코드 반려) 경로도 review_infra_failed 와 동일하게
워크트리를 즉시 삭제하지 않고, 잘린 diff 는 전량을 파일로 남기는 계약을
스크립트 텍스트 수준에서 고정한다.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _runner_script() -> str:
    return (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")


def _review_failed_branch(script: str) -> str:
    # review_verdict != APPROVE 처리 블록 시작부터 승인 상태 전이 직전까지.
    start = script.index("if [[ \"$review_verdict\" != \"APPROVE\" ]]; then")
    end = script.index("db_update \"UPDATE pipeline_jobs SET phase='awaiting_approval'", start)
    return script[start:end]


def test_review_failed_does_not_force_delete_worktree_immediately():
    branch = _review_failed_branch(_runner_script())

    # 인프라 장애 분기(review_infra_failure==true)는 원래부터 삭제하지 않았다.
    assert "WORKTREE_PRESERVED_FOR_REREVIEW" in branch
    # 코드 반려(review_infra_failure==false) 분기도 더 이상 즉시 삭제하지 않는다.
    assert "WORKTREE_PRESERVED_REJECTED" in branch
    assert "_preserve_worktree_patch \"$job_id\" \"$worktree_dir\"" in branch
    # 반려 분기에 남아있던 git worktree remove --force 즉시삭제 호출이 없어야 한다.
    assert 'git worktree remove "$worktree_dir" --force' not in branch


def test_review_failed_notification_includes_worktree_path_and_retention():
    branch = _review_failed_branch(_runner_script())

    assert "AI 리뷰 미통과로 승인 대기 차단" in branch
    assert "${ARTIFACT_MAX_AGE_HOURS}시간 보존 후 자동 회수됩니다: ${worktree_dir}" in branch


def test_review_failed_retention_is_env_configurable_with_24h_default():
    script = _runner_script()

    assert 'ARTIFACT_MAX_AGE_HOURS="${ARTIFACT_MAX_AGE_HOURS:-24}"' in script


def test_existing_stale_worktree_reaper_recovers_preserved_worktrees():
    script = _runner_script()

    # review_failed 로 보존된 워크트리는 공통 안전 검사기로만 회수한다.
    assert "_cleanup_old_artifacts()" in script
    cleanup_fn = script[script.index("_cleanup_old_artifacts()"):]
    cleanup_fn = cleanup_fn[: cleanup_fn.index("\n}\n") + 3]
    assert 'timeout 90 bash "$_reclaim_script" || true' in cleanup_fn
    assert "reclaim_runner_worktrees.sh" in cleanup_fn
    assert "git worktree remove" not in cleanup_fn
    assert "rm -rf" not in cleanup_fn
    assert "STALE_WORKTREE_CLEANUP" in cleanup_fn


def test_full_diff_is_persisted_when_db_diff_is_truncated():
    script = _runner_script()

    assert "_persist_full_diff_if_truncated()" in script
    fn = script[script.index("_persist_full_diff_if_truncated()"):]
    fn = fn[: fn.index("\n}\n") + 3]

    assert '${#full_diff} -gt ${#stored_diff}' in fn
    assert 'local log_dir="/root/aads/aads-server/logs/runner-diff"' in fn
    # 패치는 워크트리 밖에만 쓴다 — 워크트리에 쓰면 대상 저장소 커밋에 끼어든다(2026-10-08 NTV2).
    assert "worktree_patch" not in fn
    assert '> "$worktree_dir' not in fn
    assert 'local log_patch="${log_dir}/${job_id}.patch"' in fn
    assert "FULL_DIFF_TRUNCATED" in fn


def test_full_diff_persist_is_invoked_before_ai_review_uses_git_diff():
    script = _runner_script()

    ai_review_idx = script.index("# ═══ AI Reviewer 단계")
    call_idx = script.index('_persist_full_diff_if_truncated "$job_id" "$worktree_dir" "$pre_exec_sha" "$_current_head" "$git_diff"')
    review_verdict_idx = script.index('local review_verdict="APPROVE"')
    # 호출은 AI Reviewer 섹션 헤더 뒤, 실제 리뷰 로직(review_verdict 초기화)보다 앞이어야
    # git_diff 가 리뷰에 쓰이기 전에 전량 보존이 끝난다. 또한 no_changes 재확인 루프
    # 추출 블록(test_pipeline_runner_nochanges_guard_r5.py) 밖에 있어야 한다 — 그 블록은
    # 헤더 코멘트 앞까지만 잘라 별도 harness 에서 실행하므로, 헤더보다 앞에 두면
    # 스텁되지 않은 함수 호출로 harness 가 exit 127 로 깨진다.
    assert ai_review_idx < call_idx < review_verdict_idx


def test_local_runner_template_stays_synced_after_this_change():
    primary = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")
    local_template = (ROOT / "scripts" / "pipeline-runner.sh.local").read_text(encoding="utf-8")

    assert local_template == primary
