# Chat interruption owner/recovery incident v2

Date: 2026-09-08 19:18 KST

## Scope

This repair covers AADS chat execution ownership, automatic resume, late terminal callbacks, partial response retention, and automatic runner-reaction delivery. It does not mutate existing production messages or execution rows.

## Measured incident evidence

- In the six hours before the release, execution status counts were 77 completed, 31 interrupted, 6 retrying, and 4 running.
- Five executions in that window were classified `resume_exhausted` even though `retry_count=0`.
- Session `ac5278a7-2f13-4cd7-9aa1-83d41fb23c97` was running with retry count 0 at the pre-release sample.
- Session `bc68dba5-8fe1-48b7-b6df-c0502352ae3c` was retrying with retry count 2 and `watchdog_timeout` at the pre-release sample.
- Runner `runner-ce06a4ec` reproduced the lease/epoch races but failed review because a live finance collector changed `app/data/yeoljeong_finance/.bank_auto_collect.lock` inside its worktree. That runtime lock file is unrelated and is not included in this repair.

## Root causes

1. The resume lease heartbeat started only after `_RESUME_SEMAPHORE` admission. A worker could lose its lease while waiting in the queue and later misreport the owner/epoch rejection as retry exhaustion.
2. Retry count was charged once before a loop that could issue multiple model calls. The database counter therefore did not represent actual model starts.
3. `_mark_execution_interrupted` changed the assistant message before the execution status/epoch mutation was atomically accepted. A late callback could overwrite a completed reply.
4. A missing terminal `done` event was accepted solely because text length exceeded a threshold. Truncated output could be stored as completed.
5. The failure handler started from the original partial instead of the newest same-execution in-memory/checkpointed content.
6. Automatic runner notifications used process-local activity as the principal gate. A notification from another API slot could supersede a live user reply.

## Implemented controls

- Claim and heartbeat the execution lease before waiting for the resume semaphore; check the lost-lease event after queue, cooldown, retry waits, and each stream event.
- Split `ResumeFencedOut` from `ResumeAttemptLimitExceeded`; charge retry budget atomically immediately before each real model call.
- Atomically transition an eligible execution to interrupted with an owner-epoch predicate before changing any message. All terminal states are immutable to late callbacks.
- Require an explicit terminal `done` event, treat empty-done and missing-done streams as bounded retries, and checkpoint meaningful partial content between retries.
- Preserve only the newest partial belonging to the same execution.
- Check the database current execution and live lease before automatic reactions; persist and deduplicate blocked reactions in `chat_deferred_reactions`.
- Forward the claimed owner epoch through startup resume cancellation/error callbacks.

## Validation before release

- Python syntax compile: passed for `app/services/chat_service.py` and `app/main.py`.
- Focused regression suites: 123 passed, one existing FastAPI `regex` deprecation warning.
- New owner/recovery regression suite: 11 passed.
- Ruff undefined/duplicate/unused checks: passed.
- `git diff --check`: passed.

## Post-release success criteria

Monitor for at least five minutes after Blue/Green cutover and require:

- both API slots healthy and on the same image digest;
- routed and direct health checks successful;
- zero new `retry_count=0 AND interrupt_category='resume_exhausted'` executions;
- zero interruption messages created after their execution `completed_at`;
- fenced workers exit with `resume_worker_fenced_out` and do not terminalize the newer owner;
- automatic reactions remain pending while a valid user-execution lease exists.

Rollback is routing back to the previous healthy API image if routed health or P0/P1 monitoring fails.
