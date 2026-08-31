-- Blue/Green execution ownership and durable automatic-reaction handoff.
-- Safe to run repeatedly during rolling deploys.

ALTER TABLE chat_turn_executions
    ADD COLUMN IF NOT EXISTS owner_instance TEXT;

ALTER TABLE chat_turn_executions
    ADD COLUMN IF NOT EXISTS owner_epoch BIGINT NOT NULL DEFAULT 0;

ALTER TABLE chat_turn_executions
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;

ALTER TABLE chat_turn_executions
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;

ALTER TABLE chat_turn_executions
    ADD COLUMN IF NOT EXISTS resume_model_override VARCHAR(100);

CREATE INDEX IF NOT EXISTS idx_chat_turn_executions_expired_lease
    ON chat_turn_executions(lease_expires_at, updated_at)
    WHERE status IN ('running', 'retrying') AND completed_at IS NULL;

CREATE TABLE IF NOT EXISTS chat_deferred_reactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    system_message TEXT NOT NULL,
    ohvis_task_id TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    claimed_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_chat_deferred_reactions_pending
    ON chat_deferred_reactions(status, created_at)
    WHERE status IN ('pending', 'claimed');
