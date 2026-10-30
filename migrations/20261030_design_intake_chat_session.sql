BEGIN;

ALTER TABLE design_intake_sessions ADD COLUMN IF NOT EXISTS chat_session_id UUID;
CREATE INDEX IF NOT EXISTS idx_design_intake_sessions_tenant_chat_created
    ON design_intake_sessions (tenant_id, chat_session_id, created_at DESC);

COMMIT;
