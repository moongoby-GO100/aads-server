BEGIN;
ALTER TABLE chat_turn_executions DROP COLUMN IF EXISTS response_mode;
COMMIT;
