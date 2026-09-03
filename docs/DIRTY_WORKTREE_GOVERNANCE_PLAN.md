# Dirty Worktree Governance Plan

## Current Policy

- Every file-level change must have `repo`, `file_path`, `owner`, `session_id`, `task_id`, `source_tool`, `git_status`, and lifecycle `status`.
- Runtime state and generated cache files are excluded from commit ownership:
  `.active_container`, `.active_port`, `*.tsbuildinfo`, `*.bak`, `app/data/**/*.jsonl`, and `app/data/**/*.lock`.
- Stale ledger rows are not left as blocking dirty state. If a file is clean in `git status`, it is marked `reconciled_clean`.
- If a current dirty file is claimed by a newer snapshot, older dirty owners for the same file are marked `superseded_owner`.

## Automation Gates

1. `dirty-snapshot`: run `scripts/sync_workspace_change_ledger.py --session-id <session> --owner <owner> --task-id <task> --claim-path <path>` before commit planning. In a mixed dirty worktree, pass one `--claim-path` for each file owned by the current task.
2. `ownership-gate`: block automatic commit when any committable dirty file has `owner=''`, `task_id=''`, or `UNKNOWN-PREEXISTING-DIRTY`. Diagnostic snapshots may use `--allow-empty-task-id`, but commit/push/deploy automation must not.
3. `scope-gate`: group commit candidates by task id and repo. Do not mix unrelated tasks in one commit.
4. `test-gate`: run the task-specific test set and `git diff --check` for candidate files.
5. `push-gate`: push only from a clean, committed worktree or an isolated runner worktree.
6. `deploy-gate`: for AADS API releases, use `deploy.sh bluegreen` only after push and BG preflight pass.

## Next Implementation Steps

- Add an ops endpoint that returns dirty files grouped by owner/session/task_id.
- Add a commit planner that proposes exact file groups and required tests without staging anything.
- Add an approval-backed automation endpoint for `commit -> push -> bluegreen deploy`.
- Add a local pre-commit hook that rejects runtime files, cache files, and unrelated task groups.

## Automation Timing Policy

- During active coding: record or resync dirty ownership after each tool/file batch, but do not auto-commit while tests are still pending.
- Before commit: run dirty snapshot, group files by `task_id`, run candidate tests, and stage only one task group.
- Before push: require `main...origin/main` to be current and the committed file group to match the ownership ledger.
- Before BG deploy: require a clean committed release worktree, pushed SHA, `deploy.sh bluegreen`, direct health, routed health, same-image standby, and five-minute P0/P1 monitoring.
