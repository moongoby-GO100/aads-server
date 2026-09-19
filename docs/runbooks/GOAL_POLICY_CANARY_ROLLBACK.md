# Goal policy canary rollback

## Trigger

Rollback immediately when a canary produces any privilege expansion, A3 AUTO,
unmasked secret, revoke/kill-switch bypass, missing decision evidence, or an
unexpected increase in manual override or scope-miss counts.

## Safe sequence

1. Stop promotion. Do not edit an existing policy version: versions and audit
   evidence are append-only.
2. Call `POST /api/v1/goal-policy/{current_policy_id}/rollback` with the last
   reviewed `rollback_policy_id` and a non-empty incident reason.
3. Confirm that the response points to a new immutable policy version. A
   rollback to `audit_only` is deliberately fail-closed and permits no AUTO.
4. Confirm grants issued from the rejected policy are `stale` with
   `revoke_reason=policy_rollback` and that executor fence checks reject queued
   work before execution.
5. Verify `goal_policy_promotion_events` contains one append-only `rollback`
   event with source, result, rollback source, actor, and reason.
6. Re-run historical replay. Promotion can restart only when
   `privilege_expansion_count=0`, masking evidence is present, and operational
   grant/use/outbox/event counts are unchanged by replay.

## Verification queries

Use tenant-scoped reads for the resulting version, promotion event, and stale
grants. Never delete simulation or promotion evidence. Data rollback means a
new policy revision plus grant staleness; it never rewrites prior decisions.
