-- Track model retry/fallback routing for chat stream executions.
ALTER TABLE chat_turn_executions
    ADD COLUMN IF NOT EXISTS fallback_chain JSONB NOT NULL DEFAULT '[]'::jsonb;
