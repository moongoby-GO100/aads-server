# AADS P0 Wrap - chat lease, viewport, relay capacity, and release safety

Date: 2026-08-31 KST

## Outcome

- Relay runtime and the public capacity API now report 15 slots.
- Dashboard release `7ba256109adb` is on both slots and contains the chat changes from ancestor `979e127d9cef`; the live bundle shows the relay denominator, pending target, and transition state.
- Chat scroll restoration now rejects transient height collapse, does not learn programmatic restore positions as user positions, and persists a message anchor before version refresh.
- Server recovery uses DB-fenced ownership and idempotent placeholder repair. Commits are pushed through `cc877a14a5c7`.
- Clean release contexts and build-once/no-build slot rollout are enforced by repository and global rules.

## Root causes closed

1. Blue and Green could both believe they owned one execution because ownership was process-local.
2. Recovery could insert a second assistant row for an execution already holding `interrupted_partial`.
3. A programmatic scroll restore could record a temporary top position, and version refresh had no durable message anchor.
4. Relay configuration could target a higher limit while the old process still reported its startup-time semaphore size.
5. Dirty worktree and `.venv-playwright` content inflated the build context and weakened release provenance.

## Production evidence

- Relay startup sample: 4 active, 11 available, 4/4 acquisitions, zero timeouts.
- Later load sample: 9 active, 6 available, 38/38 acquisitions, zero timeouts.
- Public and both local relay-capacity endpoints reported `max_concurrent=15`.
- Dashboard Blue/Green images match release `7ba256109adb`; bundle inspection confirms `전환대기` and `aads:before-version-refresh`.
- Clean server build context was 83.22 MB versus the previous approximately 1.6 GB.
- Server image `aads-server:cc877a14a5c7` built successfully from committed HEAD.

## Verification

- Chat/recovery regression: 77 passed.
- Execution lease/release contract checks: passed.
- Dashboard TypeScript/build checks: passed.
- Post-build `/health/live`: both API slots HTTP 200, 13-49 ms.
- Public relay capacity: HTTP 200, 0.61 seconds in the recovery sample.

## Remaining rollout gate

The inactive Blue API slot still receives valid, heartbeating executions during fallback/recovery traffic. The prebuilt server image must not replace that slot until `/api/v1/ops/active-streams` reports zero. After cutover, require five continuous minutes of public health, relay-capacity, log, lease, and identical-image checks before final certification.
