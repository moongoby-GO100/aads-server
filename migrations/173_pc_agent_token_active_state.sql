-- Reconcile the PC Agent token contract used by app.api.pc_agent._verify_token_db.
-- Existing tokens remain enabled; operators can explicitly revoke a token by
-- setting is_active = FALSE without deleting its audit history.
ALTER TABLE kakao_pc_agent_tokens
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_kakao_pc_agent_tokens_active_owner
    ON kakao_pc_agent_tokens (is_active, user_id, tenant_id, id DESC);
