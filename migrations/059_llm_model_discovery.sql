ALTER TABLE llm_models
    ADD COLUMN IF NOT EXISTS execution_model_id VARCHAR(200),
    ADD COLUMN IF NOT EXISTS discovery_source VARCHAR(80) NOT NULL DEFAULT 'template',
    ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retired_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS verification_status VARCHAR(32) NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS pricing JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS is_selectable BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS is_executable BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE llm_models
SET execution_model_id = COALESCE(execution_model_id, metadata->>'execution_model_id', model_id),
    last_seen_at = COALESCE(last_seen_at, updated_at),
    verification_status = CASE
        WHEN is_active THEN 'verified'
        ELSE verification_status
    END,
    is_selectable = CASE
        WHEN metadata->>'retired' = 'true' THEN FALSE
        ELSE is_selectable
    END,
    is_executable = is_active
WHERE execution_model_id IS NULL
   OR last_seen_at IS NULL
   OR is_executable <> is_active;

CREATE TABLE IF NOT EXISTS llm_model_discovery_runs (
    id SERIAL PRIMARY KEY,
    provider VARCHAR(50) NOT NULL,
    status VARCHAR(32) NOT NULL,
    discovered_count INT NOT NULL DEFAULT 0,
    active_count INT NOT NULL DEFAULT 0,
    error TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    triggered_by VARCHAR(100) NOT NULL DEFAULT 'system',
    reason VARCHAR(200) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_model_discovery_runs_provider
    ON llm_model_discovery_runs(provider, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_models_discovery_status
    ON llm_models(provider, discovery_source, verification_status, is_executable);
