-- SaaS admin audit: attribute newly created chat sessions to the login user.
ALTER TABLE chat_sessions
  ADD COLUMN IF NOT EXISTS user_id TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated
  ON chat_sessions(user_id, updated_at DESC)
  WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_chat_sessions_tenant_user_updated
  ON chat_sessions(tenant_id, user_id, updated_at DESC)
  WHERE user_id IS NOT NULL;
