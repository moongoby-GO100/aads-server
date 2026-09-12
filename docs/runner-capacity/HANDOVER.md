# AADS remote runner capacity

CEO request: contabo14 permits 20 concurrent coding jobs; cafe24_114 permits
10 across its general and LiteLLM services combined.

## Implementation

- `scripts/pipeline-runner.sh` and its `.local` mirror accept
  `MAX_CONCURRENT_SERVER`. The claim gate counts running/claimed jobs in the
  service's `RUNNER_PROJECTS`, across all engines and tenants. All services on
  the same host must use the same project scope and capacity lock file.
- A host-local flock covers the count and committed queued-to-claimed update.
  Invalid limits, absent scope, invalid counts, and DB errors prevent claims.
- Configured hosts replace the legacy cross-server global throttle with this
  server gate. Unconfigured hosts retain their existing behavior. Approval,
  review, dependency, and model-selection policy are unchanged.
- The API continues accepting jobs into the queue. Its legacy project-lock
  display is advisory; execution capacity is enforced by the shell claim gate.
- `runner-capacity.contabo14.conf`: server/project limits 20/20.
- `runner-capacity.cafe24_114.conf`: server/project limits 10/10.
  Install as `/etc/systemd/system/<runner-service>.d/50-capacity.conf`.
  Dedicated drop-ins survive canonical base-unit synchronization.

## Rollout and rollback

Validate and push the intended files only. Hold the existing runner-sync lock
during installation to prevent a timer-driven restart of busy workers. Back up
the remote runner script. Drain existing work before restarting only the
affected runner services; do not restart the application API. Verify startup
`SERVER_CAPACITY` log, process environment, service health, and script digest.

Rollback: restore the backed-up script, remove only the newly installed
`50-capacity.conf` (or restore its prior backup), daemon-reload, and restart the
affected runners after their work drains. Coordinate the canonical source
with the sync timer so it does not undo the rollback.

## Validation at preparation

2026-09-12 20:58:39 KST (host date): shell syntax and diff checks passed.
`pytest -q tests/unit/test_pipeline_runner_server_capacity.py
tests/unit/test_pipeline_runner_script_guards.py
tests/unit/test_pipeline_runner_remote_sync.py`: 33 passed; one pre-existing
unknown `asyncio_mode` pytest configuration warning. The new tests exercise
concurrent claimers at both requested limits and DB failure/invalid input.
No production 20/10-job load test is submitted. Production rollout evidence
will be recorded in the AADS canonical handover after verification.

## Production rollout evidence

- 2026-09-12 21:00:56 KST: cafe24_114 general and LiteLLM runner services
  started with `MAX_CONCURRENT_SERVER=10`, `MAX_CONCURRENT_PER_PROJECT=10`,
  and the shared `SF,NTV2,NAS` project scope. Both services were active.
- 2026-09-12 21:09:08 KST: after the in-flight GO100 job exited, the
  contabo14 runner restarted with `MAX_CONCURRENT_SERVER=20`,
  `MAX_CONCURRENT_PER_PROJECT=20`, and project scope `GO100`. The service was
  active; no API or trading service was restarted.
- The canonical and both remote runner scripts had SHA-256
  `abf1a46e2a7c8d7e17b26c76c1d3c3f7988c91eb0ad8a29ec1430ef40477304c`.
- Production saturation was not induced. Capacity behavior is covered by the
  33 focused concurrency, script-guard, and remote-sync tests recorded above.
