# Blue/Green Release Gates

The canonical AADS-wide rules are `/root/aads/AGENTS.md`. This document records the server-side release implementation.

## Required sequence

1. Resolve the active and candidate slots and wait for a busy candidate to
   drain before rebuilding it. If it does not drain inside the configured
   window, reject the deployment unless an explicitly approved emergency
   override is present.
2. Tag and build exactly one image with `AADS_RELEASE_SHA`.
3. Start the candidate with `--no-build` and pass direct health checks.
4. Acquire the shared nginx lock, switch the upstream and active-slot marker, verify direct and routed health, then release the lock.
5. Let the previous slot drain. The drain counter is slot-local: it counts only
   executions owned by that container's DB lease (`owner_instance`) with a live
   lease/heartbeat. Peer-slot DB rows and global placeholders must not block a
   standby sync.
6. Start the previous slot with `--no-build` from the same release tag and assert
   that both container image digests match. This is a certification gate, not a
   background best-effort task.
7. Run QA and monitor the external health/error path for at least five minutes before certifying completion.

Chat execution ownership follows the same handoff principle: only the DB lease holder may mutate or complete an execution. Blue/Green process-local memory is not authoritative.

## Runtime immutability

API blue/green containers must run application code from the release-SHA image.
Production compose may mount runtime state such as `app/data`, `app/static`,
generated media, active-slot markers, vault key, browser state, and ChromaDB,
but it must not mount `/root/aads/aads-server/app` over `/app/app` or
`/root/aads/aads-server/scripts` over `/app/scripts`. Full source bind mounts
make dirty worktree changes bypass the release image and invalidate same-digest
certification.
