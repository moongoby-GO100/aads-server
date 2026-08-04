# AADS Direct Work Dependency Policy v1.0
_Applied: 2026-07-31 12:03 KST_

## Summary

AADS has two execution paths that can modify the same project:

1. Pipeline Runner jobs, recorded in `pipeline_jobs`.
2. Chat-direct work, recorded in `chat_workspace_change_ledger` when tools use the
   workspace change tracker.

Runner jobs already support `parallel_group`, `depends_on`, duplicate detection,
file-conflict auto dependency, worktree execution, and actual changed file
recording. Chat-direct work has session ledger and Git finalization locks, but it
does not yet automatically become a `pipeline_jobs.depends_on` parent.

Therefore, until the automated Direct Work Dependency Gate is implemented in
code, every chat-direct code or DB change must follow this operating policy.

## Current System Facts

| Area | Current Mechanism | Limit |
|---|---|---|
| Runner dependency | `pipeline_jobs.depends_on`, `parallel_group`, file-conflict scan | Applies only to work submitted as runner jobs |
| Runner isolation | clean worktree per job, Redis work lock, `actual_changed_files` | Requires runner submission path |
| Chat-direct ledger | `chat_workspace_change_ledger` by session/project/repo/file | Ledger is not automatically joined to runner dependency graph |
| Chat-direct finalization | `git_project_lock(project:repo)` around git add/commit/push | Protects final git step, not earlier file editing intent |
| DB work | read-only query tools and migration files | Direct writes must be explicitly scoped and reported |

## Operating Policy

### 1. Default Routing

| Request Type | Required Route | Reason |
|---|---|---|
| Multi-file code change | Pipeline Runner | Runner can set dependencies and isolate worktrees |
| Same file touched by another active job/session | Pipeline Runner with `depends_on` or wait | Prevents cross-session overwrite |
| DB schema/data change | Migration + Runner, unless CEO explicitly requests immediate direct DB change | Keeps rollback and audit path |
| Deploy/restart/push | Runner approval flow or explicit CEO approval | Operational impact and rollback needed |
| Single-file XS hotfix | Chat-direct allowed after dependency preflight | Keeps urgent fixes fast |
| Read-only inspection/report | Chat-direct allowed | No mutation conflict |

### 2. Mandatory Preflight Before Chat-Direct Mutation

Before direct file or DB mutation from a chat session, the operator must check:

1. `git status --short` for the target repo.
2. Active runner jobs for the same project in `pipeline_jobs`.
3. Active runner `target_files`/`actual_changed_files` overlap when available.
4. Dirty entries in `chat_workspace_change_ledger` for the same project/repo/file
   from other sessions.
5. Whether the requested change is XS/single-file and reversible.

Decision:

| Gate | Condition | Action |
|---|---|---|
| GREEN | No active runner overlap and no other-session dirty overlap | Direct edit allowed |
| YELLOW | Dirty work exists in same repo but different files | Direct edit allowed only with scoped file list and final report |
| RED | Same file/path conflict or active runner likely touching same area | Submit Runner with `depends_on`, or wait |
| BLOCK | DB destructive/risky write, deploy/restart/push without approval, secret path | Stop and request explicit CEO approval or use approved runner flow |

### 3. Chat-Direct Execution Rules

When GREEN/YELLOW allows direct work:

1. Touch only the requested file(s).
2. Do not stage unrelated dirty files.
3. Record/maintain session ledger status if the tool path supports it.
4. Validate locally with the narrowest meaningful command.
5. Report commit, push, deploy, document, and uncompleted state separately.
6. If conflict is detected mid-work, stop direct editing and convert to Runner
   or ask CEO to choose.

### 4. DB Change Rules

DB changes made directly from chat are allowed only when all are true:

1. CEO explicitly requested immediate policy/config/data reflection.
2. Change is non-destructive and reversible.
3. Transaction or idempotent upsert is used.
4. Before/after SELECT verification is recorded.
5. Migration file or handover record is created when the change is policy or schema.

DROP, TRUNCATE, broad delete, secret exposure, and force operations remain blocked.

### 5. Runner Submission Rules From Chat

If direct work is not GREEN/YELLOW:

1. Use `pipeline_runner_submit` for one job, or batch submit for independent
   sub-jobs.
2. Put parallel-safe jobs in the same `parallel_group`.
3. Use `depends_on_key` for same-file or ordered work inside a batch.
4. Include observed file paths, DB tables, and validation requirements in the
   instruction.
5. Report `job_id`, `parallel_group`, `depends_on`, size, and expected validation.

## Automation Backlog

| Priority | Item | Completion Criteria |
|---|---|---|
| P0 | Add Direct Work Dependency Gate service | API checks runner jobs + dirty ledger before direct mutation |
| P0 | Enforce gate in `tool_executor` write/patch paths | Same-file conflicts are blocked or converted to runner |
| P1 | Add `target_files` ledger for chat-direct changes | Runner conflict scan can see chat-direct pending files |
| P1 | Add dashboard conflict panel | CEO can see dirty sessions and blocking files |
| P2 | Add auto-finalize suggestion | Chat can suggest commit/push or cleanup at turn end |

## Completion Definition

This policy is considered operationally reflected when:

1. This document exists in the AADS repo.
2. The L1 prompt asset `global-direct-work-dependency-gate` is enabled in
   `prompt_assets`.
3. The current chat provenance after the next response includes the asset slug
   or, at minimum, the row is query-verified and ready for prompt compilation.
4. HANDOVER records the policy and verification.

