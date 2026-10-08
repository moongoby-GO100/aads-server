# AADS Server Agent Rules

Read and obey `/root/aads/AGENTS.md` before changing or deploying this repository.

For every API release, `deploy.sh bluegreen` is the default and must enforce: one image build per release SHA, `--no-build` slot starts, candidate health before the nginx lock, DB-fenced execution ownership, same-digest standby synchronization, rollback on routed-health failure, and five-minute P0/P1 monitoring before completion is reported.

Never overwrite unrelated dirty files, restart the active API directly, deploy the full compose stack for an app change, or use `git commit --no-verify`.

Target-slot drain policy (CEO directive 2026-10-06): `deploy.sh` waits up to `AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT` (default 180s) for the candidate slot's active streams, then proceeds even if some remain (`AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=proceed`, the default). The remaining count and session ids are logged, written to the control audit as `target-drain`/`proceeded_busy`, and kept in `deploy_runs.error_summary`. Blocking on a busy slot is opt-in via `AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=block`; `AADS_DEPLOY_ALLOW_BUSY_TARGET=true` still skips the wait entirely. Health, image-verification and rollback gates are unchanged.

## 핸드오버 기록 (R-001, CEO 승인 2026-10-08 개정)

- 핸드오버는 DB(`handover_write` 또는 `POST /api/v1/handovers`)에만 기록한다. 정본은 `project_handover_entries` 다.
- `HANDOVER.md` 파일을 수정하지 않는다. 파일은 읽기 호환용으로만 남긴다 — 삭제하지 않는다.
- 러너는 `HANDOVER.md`, `*/HANDOVER.md`, `docs/HANDOVER.md` 변경을 커밋 직전에 스테이징에서 제외한다(워크트리 파일은 보존, `handover_md_excluded` 이벤트 기록). 예외는 `RUNNER_ALLOW_HANDOVER_MD=1` 뿐이다.
- 러너는 작업이 `awaiting_approval`/`done` 이 될 때 `entry_key=runner:<job_id>` 로 결과를 DB 에 upsert 하고 다시 읽어 확인한다. 실패하면 `handover_db_write_failed` 이벤트를 남기고 작업 결과에 "핸드오버 DB 미기록"을 표시한다(작업 자체는 실패시키지 않는다).
- 이유: 2026-10-08 origin/main 커밋 10건 중 6건이 `HANDOVER.md` 를 수정해 병행 러너가 stale_base 를 반복했다.
