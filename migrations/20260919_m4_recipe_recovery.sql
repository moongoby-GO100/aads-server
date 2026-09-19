-- M4 R3: bounded, tenant-scoped browser recipe recovery evidence.
-- The browser_recipes registry remains the sole canonical recipe/site definition.
CREATE TABLE IF NOT EXISTS browser_recipe_recovery_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    recipe_id TEXT NOT NULL,
    recipe_version TEXT NOT NULL,
    site_key TEXT NOT NULL,
    page_key TEXT NOT NULL,
    skill_key TEXT NOT NULL DEFAULT '',
    skill_version TEXT NOT NULL DEFAULT '',
    run_id UUID NULL REFERENCES browser_recipe_runs(id) ON DELETE SET NULL,
    idempotency_key TEXT NOT NULL,
    failure_class TEXT NOT NULL CHECK (failure_class IN (
        'selector_changed', 'aria_signature_mismatch', 'login_expired',
        'network_transient', 'unknown'
    )),
    recovery_action TEXT NOT NULL CHECK (recovery_action IN (
        'rediscover', 'retry', 'human_gateway', 'quarantined'
    )),
    retry_attempt INTEGER NOT NULL DEFAULT 0 CHECK (retry_attempt >= 0 AND retry_attempt <= 2),
    retry_limit INTEGER NOT NULL DEFAULT 2 CHECK (retry_limit = 2),
    candidate_artifact_version_id UUID NULL REFERENCES browser_learned_artifact_versions(id) ON DELETE SET NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    human_guidance TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_browser_recipe_recovery_scope
    ON browser_recipe_recovery_events
       (tenant_id, recipe_id, recipe_version, site_key, page_key, skill_key, skill_version, created_at DESC);
