-- Migration 027: Phase 1 DB Integrity Fixes
-- Date: 2026-03-12
-- Issues: F3, F12, F20, F21

-- F3: Orphan session_notes cleanup (102 rows deleted)
-- DELETE FROM session_notes WHERE session_id NOT IN (SELECT id::text FROM chat_sessions)
--   AND session_id NOT IN (SELECT session_id FROM ceo_chat_sessions);

-- F12: timestamp without time zone → timestamptz (42 columns)
ALTER TABLE approval_queue ALTER COLUMN executed_at TYPE timestamptz;
ALTER TABLE approval_queue ALTER COLUMN requested_at TYPE timestamptz;
ALTER TABLE approval_queue ALTER COLUMN responded_at TYPE timestamptz;
ALTER TABLE ceo_chat_messages ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE ceo_chat_sessions ALTER COLUMN ended_at TYPE timestamptz;
ALTER TABLE ceo_chat_sessions ALTER COLUMN started_at TYPE timestamptz;
ALTER TABLE ceo_facts ALTER COLUMN updated_at TYPE timestamptz;
ALTER TABLE ceo_session_summaries ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE circuit_breaker_state ALTER COLUMN cooldown_until TYPE timestamptz;
ALTER TABLE circuit_breaker_state ALTER COLUMN last_failure_at TYPE timestamptz;
ALTER TABLE circuit_breaker_state ALTER COLUMN opened_at TYPE timestamptz;
ALTER TABLE circuit_breaker_state ALTER COLUMN updated_at TYPE timestamptz;
ALTER TABLE debate_logs ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE error_log ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE error_log ALTER COLUMN first_seen TYPE timestamptz;
ALTER TABLE error_log ALTER COLUMN last_seen TYPE timestamptz;
ALTER TABLE error_log ALTER COLUMN resolved_at TYPE timestamptz;
ALTER TABLE experience_memory ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE experience_memory ALTER COLUMN last_accessed TYPE timestamptz;
ALTER TABLE experience_memory ALTER COLUMN updated_at TYPE timestamptz;
ALTER TABLE go100_user_memory ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE go100_user_memory ALTER COLUMN expires_at TYPE timestamptz;
ALTER TABLE lessons ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE lessons ALTER COLUMN updated_at TYPE timestamptz;
ALTER TABLE monitored_services ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE monitored_services ALTER COLUMN last_check TYPE timestamptz;
ALTER TABLE procedural_memory ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE procedural_memory ALTER COLUMN updated_at TYPE timestamptz;
ALTER TABLE project_artifacts ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE project_memory ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE project_memory ALTER COLUMN updated_at TYPE timestamptz;
ALTER TABLE project_plans ALTER COLUMN approved_at TYPE timestamptz;
ALTER TABLE project_plans ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE projects ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE projects ALTER COLUMN updated_at TYPE timestamptz;
ALTER TABLE recovery_log ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE recovery_log ALTER COLUMN executed_at TYPE timestamptz;
ALTER TABLE recovery_log ALTER COLUMN last_used TYPE timestamptz;
ALTER TABLE recovery_logs ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE strategy_reports ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE system_memory ALTER COLUMN created_at TYPE timestamptz;
ALTER TABLE system_memory ALTER COLUMN updated_at TYPE timestamptz;

-- F20: Missing indexes for frequently queried columns
CREATE INDEX IF NOT EXISTS idx_chat_messages_compacted ON chat_messages(is_compacted) WHERE is_compacted = true;
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_project ON pipeline_jobs(project);
CREATE INDEX IF NOT EXISTS idx_directive_lifecycle_status ON directive_lifecycle(status);
CREATE INDEX IF NOT EXISTS idx_error_log_resolved ON error_log(resolved_at) WHERE resolved_at IS NULL;

-- F21: chat_drive_files FK → CASCADE (workspace 삭제 시 파일 정리)
ALTER TABLE chat_drive_files DROP CONSTRAINT IF EXISTS chat_drive_files_workspace_id_fkey;
ALTER TABLE chat_drive_files ADD CONSTRAINT chat_drive_files_workspace_id_fkey
  FOREIGN KEY (workspace_id) REFERENCES chat_workspaces(id) ON DELETE CASCADE;
