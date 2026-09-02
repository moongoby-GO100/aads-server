-- 142: BYOK user-scoped AI API keys
-- Stores SaaS user-owned provider keys encrypted by app.core.credential_vault.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS user_api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    encrypted_key TEXT NOT NULL,
    display_name TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_user_api_keys_user_provider
    ON user_api_keys(user_id, provider);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_user_active
    ON user_api_keys(user_id, is_active);
CREATE INDEX IF NOT EXISTS idx_user_api_keys_tenant_provider
    ON user_api_keys(tenant_id, provider)
    WHERE is_active = TRUE;

COMMENT ON TABLE user_api_keys IS 'BYOK user-scoped AI API keys encrypted with Credential Vault';
COMMENT ON COLUMN user_api_keys.encrypted_key IS 'Fernet-encrypted provider API key; never expose plaintext in API responses or logs';

COMMIT;
