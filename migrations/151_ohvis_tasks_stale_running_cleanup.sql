-- 151: One-time cleanup of ohvis_tasks zombie 'running' rows.
-- Date: 2026-09-06
--
-- Context (P0 러너 완료 루프 복구):
-- 143 ohvis_tasks rows have been stuck in status='running' since as far back as
-- 2026-07-26 with zero completion reports (reported_at IS NULL) — the originating
-- runner/agent processes are long dead and will never report back. These rows
-- occupy the per-session active-task slot limit (_MAX_CONCURRENT_TASKS in
-- app/services/ohvis_task_manager.py) and show as perpetually "in progress" on
-- dashboards. Only rows stale for 24h+ are touched, so genuinely in-flight tasks
-- are left alone.
--
-- Going forward, app/services/ohvis_task_manager.mark_stale_running_tasks()
-- (invoked periodically from app/main.py) marks new occurrences as 'stale'
-- instead of letting them accumulate — this migration only remediates the
-- pre-existing backlog by force-closing it as 'done' per CEO directive.

UPDATE ohvis_tasks
SET status = 'done',
    completed_at = COALESCE(completed_at, updated_at, NOW()),
    reported_at = COALESCE(reported_at, NOW()),
    result = COALESCE(result, '{}'::jsonb) || jsonb_build_object(
        'auto_closed', true,
        'auto_closed_reason', 'migration_151_stale_running_backlog',
        'auto_closed_at', to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SSOF')
    ),
    updated_at = NOW()
WHERE status = 'running'
  AND updated_at < NOW() - INTERVAL '24 hours';
