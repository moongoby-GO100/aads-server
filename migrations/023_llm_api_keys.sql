CREATE TABLE IF NOT EXISTS llm_api_keys (
    id SERIAL PRIMARY KEY,
    provider VARCHAR(50) NOT NULL,
    key_name VARCHAR(100) NOT NULL UNIQUE,
    encrypted_value TEXT NOT NULL,
    label VARCHAR(100) DEFAULT '',
    priority INT DEFAULT 1,
    is_active BOOLEAN DEFAULT TRUE,
    rate_limited_until TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    last_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    notes TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_llm_api_keys_provider
    ON llm_api_keys(provider, is_active, priority);
