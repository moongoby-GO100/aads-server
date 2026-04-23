CREATE TABLE IF NOT EXISTS llm_models (
    id SERIAL PRIMARY KEY,
    provider VARCHAR(50) NOT NULL,
    model_id VARCHAR(150) NOT NULL,
    display_name VARCHAR(200) NOT NULL,
    family VARCHAR(80) NOT NULL DEFAULT 'general',
    category VARCHAR(80) NOT NULL DEFAULT 'general',
    supports_tools BOOLEAN NOT NULL DEFAULT FALSE,
    supports_thinking BOOLEAN NOT NULL DEFAULT FALSE,
    supports_vision BOOLEAN NOT NULL DEFAULT FALSE,
    supports_coding BOOLEAN NOT NULL DEFAULT FALSE,
    input_cost NUMERIC(12, 6),
    output_cost NUMERIC(12, 6),
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    activation_source VARCHAR(32) NOT NULL DEFAULT 'fallback',
    linked_key_name VARCHAR(100),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider, model_id),
    CONSTRAINT llm_models_activation_source_chk
        CHECK (activation_source IN ('db', 'manual', 'fallback', 'review_required')),
    CONSTRAINT llm_models_linked_key_fk
        FOREIGN KEY (linked_key_name)
        REFERENCES llm_api_keys(key_name)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_models_provider_active
    ON llm_models(provider, is_active, family);

CREATE INDEX IF NOT EXISTS idx_llm_models_model_id
    ON llm_models(model_id);

CREATE TABLE IF NOT EXISTS llm_key_audit_logs (
    id SERIAL PRIMARY KEY,
    key_id INT REFERENCES llm_api_keys(id) ON DELETE SET NULL,
    provider VARCHAR(50) NOT NULL,
    key_name VARCHAR(100) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    actor VARCHAR(100) NOT NULL DEFAULT 'system',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_key_audit_logs_key
    ON llm_key_audit_logs(key_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_key_audit_logs_provider
    ON llm_key_audit_logs(provider, created_at DESC);
