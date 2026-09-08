# runner-ce06a4ec preservation gate diagnosis

Observed: 2026-09-08 19:14:52 KST (`TZ=Asia/Seoul date`). AADS only.

## Confirmed cause

Read-only `query_database` against `code_reviews` for this job returned
`FLAG`, score approximately 0.3, `model_used=precheck`,
`failure_stage=pre_llm_preservation_gate`. The only reported issue was
deleted symbols: `_on_resume_done`, `_on_auto_resume_done`,
`_resume_lease_pump`. This is a deterministic precheck rejection, not a
failed test or an LLM's semantic review of the repair.

The complete stored `pipeline_jobs.git_diff` was retrieved in bounded
chunks (46,025 characters). All three definitions occur on both sides:
the two callbacks gain an epoch default argument; the pump is reindented.
The old regular expression only inspects removed lines. It therefore
mistakes these edits for removal. The recorded 530 additions / 160
deletions did not trigger the deletion-ratio threshold.

The unrelated finance lock appears in `actual_changed_files`, but it was
NOT the issue recorded by this review. Exclude it from repair commits.
The worker's refusal to commit was also NOT this gate's stated cause.

## Focused correction

`app/services/code_reviewer.py` now pairs unique private function
declarations within each file diff, preserving the sync/async kind.
Re-added private definitions proceed to normal semantic review, never
automatic approval. Actual removals, public/dunder functions, classes,
router decorators, cross-file moves and duplicate-name ambiguity remain
gated. Deletion-ratio and explicit file-scope checks remain in force.
This heuristic is not an AST or behavior-equivalence proof. Semantic
review still has its existing 10,000-character input limit; passing this
precheck alone cannot certify the original large chat patch.

## Validation and recovery status

- Both reviewer unit suites pass; exact final count is recorded in HANDOVER.
- Original stored diff: old matcher reports the three symbols; corrected
  matcher reports no unmatched removals and preservation precheck returns
  `None` (eligible for semantic review, not approved).
- Unit coverage includes epoch signature edits, pump reindent, real removal,
  API/class/route/dunder changes, cross-file moves, duplicates, scope and
  deletion-ratio protection. An integration test verifies semantic rejection
  is still honored after the precheck passes.
- `health_check(server='68')` maps to contabo116 and returned HEALTHY.
- `pipeline_jobs` has no queued/running/awaiting_approval AADS jobs at the
  preflight read. The original job remains `error/review_failed`; its
  dependent `runner-b39811b1` is `cancelled/blocked_dependency`.
- `/tmp/aads-chat-recovery.mfZ92G` has independent dirty changes in
  `app/main.py` and `app/services/chat_service.py`. Other sessions also have
  dirty chat-service ledger entries. Do not merge/reapply the old repair
  or revive its cancelled release concurrently with that work.
- Original worker claims of 200 passing tests and full-unit comparisons
  were not reverified here. Its former worktree is absent from git's
  registered worktrees. Main includes a separate partial chat fix,
  `1d6e97c5`; that is not proof the entire failed-job patch was recovered.

## Delivery and remaining gate

This fix is isolated on `fix/reviewer-symbol-preservation-ce06` based on
`c2c2d24b`, with only reviewer code, tests and documentation selected.
No production DB writes, runner resurrection, service restart, or deployment.
Commit/push status is reported with the actual command results.

Before release: reconcile the independently owned chat repair, review its
complete final diff and run its behavioral tests. Integrate this reviewer
fix, then release a clean pushed SHA through `deploy.sh bluegreen` with
same-image standby and five-minute P0/P1 monitoring. The original fix-job
instruction explicitly delegates deployment to a separate release job.
Rollback of this code change is an ordinary revert of its focused commit;
there is no schema migration or production state to undo in this task.
