# 2026-05-06 Pipeline Runner Audit Remediation

## Summary

- Measured at 2026-05-06 09:29-09:34 KST.
- Root cause 1: `psql` command tags such as `UPDATE 0` were emitted by `db_exec()` for `UPDATE ... RETURNING` statements with no rows. Recovery loops treated that text as a job id, causing repeated `WARN: invalid session_id` logs on 68/211/114.
- Root cause 2: several failure paths wrote `status='cancelled', phase='superseded'` for real runtime failures. This polluted completion metrics and hid deploy/runtime causes.
- Root cause 3: 211 had an older runner script that downgraded configured `codex:gpt-5.5` to `gpt-5.4` while logs/DB could still imply the configured model.

## Actions Applied

| Area | Action |
|---|---|
| DB command output | `db_exec()` now uses `psql -q -P footer=off` so empty `UPDATE ... RETURNING` results do not produce `UPDATE 0` pseudo job ids. |
| Failure state semantics | Runtime, watchdog, token, restart, shutdown, deploy lock, rollback health failures now move to `status='error'` with specific `phase/error_detail`. |
| Deploy diagnostics | `git commit` and `git push` stderr tails are preserved in `review_feedback`. |
| Actual model trace | Codex effective model is recorded separately from configured model when the runner has to normalize or fallback. |
| API visibility | `/api/v1/pipeline/jobs/{job_id}` now returns `model`, `worker_model`, `actual_model`, `size`; lock-status returns `running_count` and `max_concurrent_per_project`. |
| Stale queue cleanup | 6 stale queued jobs, 1 stale rolling_back job, 1 invalid `failed` row, and 6 mismatched `rejected_done` phases were normalized in DB. |
| Server sync | 68 and 114 runner scripts were updated and restarted. 211 script was updated on disk but not restarted because `runner-d59beba6` is actively running. |

## Current State After Cleanup

| Project | Active | Queued | Notes |
|---|---:|---:|---|
| AADS | 0 | 0 | 4 stale queued jobs were converted to error. |
| GO100 | 1 | 0 | `runner-d59beba6` is still running on 211. 2 stale queued jobs were converted to error. |
| KIS | 0 | 0 | phase/status mismatch normalized. |
| SF | 0 | 0 | phase/status mismatch normalized. |
| NTV2 | 0 | 0 | no active queue. |

## Follow-up Required

1. When `runner-d59beba6` finishes, restart 211 runner so the already-installed patched script takes effect:

```bash
systemctl restart aads-pipeline-runner
systemctl is-active aads-pipeline-runner
```

2. For `runner-d59beba6`, its current process was launched by the old 211 script. The actual CLI process is `codex ... -m gpt-5.4` even though the DB configured model is `codex:gpt-5.5`. If it completes before restart, manually interpret its actual model as `codex:gpt-5.4`.

3. Keep per-server concurrency intentionally separate:

| Server | Projects | Current max |
|---|---|---:|
| 68 | AADS | 6 |
| 211 | KIS, GO100 | 3 |
| 114 | SF, NTV2, NAS | 2 |

