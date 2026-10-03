BEGIN;
ALTER TABLE chat_sessions DROP CONSTRAINT IF EXISTS chat_sessions_effort_mode_chk;
ALTER TABLE chat_sessions DROP CONSTRAINT IF EXISTS chat_sessions_effort_manual_chk;
ALTER TABLE chat_turn_executions DROP COLUMN IF EXISTS effort_status;
ALTER TABLE chat_sessions DROP COLUMN IF EXISTS effort_updated_at;
ALTER TABLE chat_sessions DROP COLUMN IF EXISTS effort_pending;
ALTER TABLE chat_sessions DROP COLUMN IF EXISTS effort_manual;
ALTER TABLE chat_sessions DROP COLUMN IF EXISTS effort_mode;
COMMIT;
