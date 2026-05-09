# AADS HANDOVER
최종 업데이트: 2026-05-09

## 2026-05-09
- Project Change Promoter implemented: completed runner jobs and raw `memory_facts` change events can now be promoted into `architecture_decision`, `feature_change`, `api_contract`, and `data_model_change` facts.
- `workspace_preloader` now prioritizes those strategic change categories in a `최근 중요 변경 자동 인지` block so future sessions can recognize important architecture/function/API/DB changes automatically.
- Canary applied on live DB: 4 CEO workspace changes were promoted with embeddings; duplicate prevention uses `project_change_promoter` source tags.
- Operational note: scheduler registration is coded as `project_change_promoter` every 30 minutes, but the running API process must be restarted/deployed before that scheduler is active.

## 2026-05-06
- Pipeline Runner audit/remediation applied: `db_exec()` now suppresses empty `UPDATE ... RETURNING` command tags, preventing `UPDATE 0` from being misread as job ids and producing repeated `invalid session_id` logs.
- Failure semantics hardened: runtime/watchdog/token/restart/shutdown/deploy-lock/rollback failures now use `status='error'` with explicit `phase/error_detail` instead of `cancelled/superseded`.
- Actual model trace hardened: runner now records Codex effective model after CLI normalization/fallback; API job detail now exposes `model`, `worker_model`, `actual_model`, `size`.
- Chat-direct AI review diff capture hardened: `tool_executor` now quotes git paths, strips `run_remote_command` wrappers, and falls back to local `git -C ... diff` for AADS when SSH/host bridge git diff capture fails.
- DB queue cleanup performed: 6 stale queued jobs, 1 stale rolling_back job, 1 invalid `failed` row, and 6 mismatched `rejected_done` phases normalized.
- Server sync: 68 and 114 runner scripts updated and restarted. 211 script updated on disk only; do not restart until active GO100 job `runner-d59beba6` finishes.
- Technical record added: `docs/reports/20260506_RUNNER_AUDIT_REMEDIATION.md`.

## 2026-05-04
- Android Agent Galaxy Z Fold6 remote-control follow-up: `runner-4f922625` code implementation is committed in `aads-server` as `05c7dc7`; follow-up sensor JSON hardening skips non-finite values during `JSONArray` serialization.
- Chat visibility fix: `runner_response` assistant messages are no longer hidden by backend/dashboard message filters, so saved AI review/status reports remain visible in the main chat timeline.
- Pipeline Runner docs/config: AADS per-project concurrency is documented/configured as `MAX_CONCURRENT_PER_PROJECT=6`; global limit remains 10.
- Technical record added: `docs/reports/20260504_ANDROID_AGENT_CHAT_VISIBILITY_TECHNICAL.md`.
- Caution: `runner-4f922625` has finalize/deploy timeout history. Future deploy-complete reports require fresh health/API/APK download verification.

## 2026-04-24
- Phase 1-C: claude_md_merger ready, /api/v1/ops/claude-md endpoint live
