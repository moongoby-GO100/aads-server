# Blue/Green Release Gates

The canonical AADS-wide rules are `/root/aads/AGENTS.md`. This document records the server-side release implementation.

## Required sequence

1. Resolve the active and candidate slots and reject a busy candidate unless an explicitly approved emergency override is present.
2. Tag and build exactly one image with `AADS_RELEASE_SHA`.
3. Start the candidate with `--no-build` and pass direct health checks.
4. Acquire the shared nginx lock, switch the upstream and active-slot marker, verify direct and routed health, then release the lock.
5. Let the previous slot drain. Start it with `--no-build` from the same release tag and assert that both container image digests match.
6. Run QA and monitor the external health/error path for at least five minutes before certifying completion.

Chat execution ownership follows the same handoff principle: only the DB lease holder may mutate or complete an execution. Blue/Green process-local memory is not authoritative.
