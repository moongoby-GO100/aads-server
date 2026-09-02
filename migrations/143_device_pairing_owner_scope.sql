-- 143: Device pairing owner scope
-- Adds user/tenant ownership to Android/iOS/PC device pairing tokens.

BEGIN;

CREATE TABLE IF NOT EXISTS device_pairing_tokens (
    id SERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL,
    device_type TEXT NOT NULL DEFAULT 'android',
    token_hash TEXT UNIQUE NOT NULL,
    label TEXT DEFAULT '',
    created_by TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

ALTER TABLE device_pairing_tokens
    ADD COLUMN IF NOT EXISTS user_id TEXT,
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

CREATE INDEX IF NOT EXISTS idx_device_pairing_tokens_owner_recent
    ON device_pairing_tokens(user_id, tenant_id, id DESC);

COMMIT;
