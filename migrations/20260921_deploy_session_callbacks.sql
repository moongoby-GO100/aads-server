-- Bind deploy runs to their originating chat session and deliver terminal events once.
-- Additive and idempotent. Existing runs remain unbound and are never replayed.

ALTER TABLE deploy_runs
    ADD COLUMN IF NOT EXISTS chat_session_id UUID REFERENCES chat_sessions(id) ON DELETE SET NULL;
ALTER TABLE deploy_runs
    ADD COLUMN IF NOT EXISTS session_notification_status TEXT NOT NULL DEFAULT 'unbound';
ALTER TABLE deploy_runs
    ADD COLUMN IF NOT EXISTS session_notification_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE deploy_runs
    ADD COLUMN IF NOT EXISTS session_notification_owner TEXT;
ALTER TABLE deploy_runs
    ADD COLUMN IF NOT EXISTS session_notification_claimed_at TIMESTAMPTZ;
ALTER TABLE deploy_runs
    ADD COLUMN IF NOT EXISTS session_notified_at TIMESTAMPTZ;
ALTER TABLE deploy_runs
    ADD COLUMN IF NOT EXISTS session_notification_error TEXT;

CREATE INDEX IF NOT EXISTS idx_deploy_runs_session_notification_pending
    ON deploy_runs (session_notification_status, updated_at, id)
    WHERE chat_session_id IS NOT NULL
      AND status IN ('completed', 'success', 'success_partial', 'failed', 'error',
                     'blocked', 'cancelled', 'superseded')
      AND session_notification_status IN ('pending', 'processing', 'reported');

COMMENT ON COLUMN deploy_runs.chat_session_id IS
    'Originating AADS chat session that receives terminal deployment reports';
COMMENT ON COLUMN deploy_runs.session_notification_status IS
    'unbound, pending, processing, reported, notified, skipped, or failed';
