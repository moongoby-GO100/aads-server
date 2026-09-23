# AAG v2 metadata join failure — 2026-09-23

## Symptom and impact

Deploy run 5155 failed its five-minute P0/P1 gate after `aag_brief` logged
`column s.scanner_version does not exist`. The same invalid projection was in
the public `/api/v1/aag/v2/latest` reader. AAG v2 reads could fail even though
the graph and authoritative pointer were present; this also blocked unrelated
releases from certification.

## Root cause

The immutable `aag_graph_snapshots_v2` table stores deduplicated graph content.
The scan provenance fields `scanner_version`, `ruleset_digest` and
`scan_scope_digest` belong to `aag_scan_runs`. Both readers selected them from
snapshot alias `s` instead of the pointer's run. Live schema inspection found
these columns only on `aag_scan_runs`; all six current authoritative pointers
have a matching scan run. Adding columns to snapshots would break the content
deduplication boundary and require data backfill, so no schema mutation is used.

## Correction and prevention

Join `aag_scan_runs r` by pointer `run_id`, project, repository, ref, governance
scope and resolved commit, and project the three provenance fields from `r`.
Apply this to both the session-tool reader and public latest endpoint. A
regression test checks both query paths against the normalized schema. Keep the
P0/P1 deployment gate closed until the corrected image passes a real AAG read,
five-minute monitoring, and same-digest standby synchronization.

## Verification

- Read-only production DB JOIN: 6/6 authoritative pointers match their run.
- Targeted AAG unit suite: 225 passed.
- Live container: all six authoritative project snapshots loaded; the
  `aag_brief` tool and public latest-reader SQL returned successfully.
- Release 5163 (`f36e0446`): LLM health and five-minute P0/P1 monitoring
  succeeded; standby synchronization subsequently certified the run as
  `success` at 2026-09-23 13:52 KST.
- Later documentation release 5168 (`f78d9e4f`) also passed five-minute
  P0/P1 monitoring and reached `success` with matching slot image digests.
