# AADS Server Agent Rules

Read and obey `/root/aads/AGENTS.md` before changing or deploying this repository.

For every API release, `deploy.sh bluegreen` is the default and must enforce: one image build per release SHA, `--no-build` slot starts, candidate health before the nginx lock, DB-fenced execution ownership, same-digest standby synchronization, rollback on routed-health failure, and five-minute P0/P1 monitoring before completion is reported.

Never overwrite unrelated dirty files, restart the active API directly, deploy the full compose stack for an app change, or use `git commit --no-verify`.

Target-slot drain policy (CEO directive 2026-10-06): `deploy.sh` waits up to `AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT` (default 180s) for the candidate slot's active streams, then proceeds even if some remain (`AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=proceed`, the default). The remaining count and session ids are logged, written to the control audit as `target-drain`/`proceeded_busy`, and kept in `deploy_runs.error_summary`. Blocking on a busy slot is opt-in via `AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=block`; `AADS_DEPLOY_ALLOW_BUSY_TARGET=true` still skips the wait entirely. Health, image-verification and rollback gates are unchanged.
