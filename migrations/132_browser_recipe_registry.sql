-- AADS-APILESS-AUTH-AUTOMATION-P0: BrowserRecipe registry and runtime planning.
-- Additive only. No destructive statements.

CREATE TABLE IF NOT EXISTS browser_recipes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    recipe_id TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT 'v1',
    title TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    allowed_origins JSONB NOT NULL DEFAULT '[]'::jsonb,
    work_key_template TEXT NOT NULL DEFAULT '',
    runtime_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    concurrency_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    resource_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    login_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    challenge_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    navigation_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    capture_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
    parser_id TEXT NOT NULL DEFAULT '',
    upload_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
    risk_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    verifier JSONB NOT NULL DEFAULT '{}'::jsonb,
    fallbacks JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version_hash TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, recipe_id, version)
);

CREATE INDEX IF NOT EXISTS idx_browser_recipes_tenant_service
    ON browser_recipes(tenant_id, service, enabled);

CREATE INDEX IF NOT EXISTS idx_browser_recipes_version_hash
    ON browser_recipes(tenant_id, version_hash);

CREATE TABLE IF NOT EXISTS browser_recipe_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    recipe_id TEXT NOT NULL,
    recipe_version TEXT NOT NULL DEFAULT 'v1',
    recipe_hash TEXT NOT NULL DEFAULT '',
    task_id UUID NULL REFERENCES browser_tasks(id) ON DELETE SET NULL,
    work_key TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    runtime TEXT NOT NULL DEFAULT 'auto',
    status TEXT NOT NULL DEFAULT 'queued',
    concurrency_key TEXT NOT NULL DEFAULT '',
    resource_claim JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NULL,
    completed_at TIMESTAMPTZ NULL,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_browser_recipe_runs_active
    ON browser_recipe_runs(tenant_id, status, concurrency_key, created_at DESC)
    WHERE status IN ('queued', 'running', 'approval_required');

CREATE INDEX IF NOT EXISTS idx_browser_recipe_runs_task
    ON browser_recipe_runs(task_id);

CREATE TABLE IF NOT EXISTS browser_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    task_id UUID NULL REFERENCES browser_tasks(id) ON DELETE SET NULL,
    recipe_run_id UUID NULL REFERENCES browser_recipe_runs(id) ON DELETE SET NULL,
    artifact_type TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    storage_uri TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_browser_artifacts_task
    ON browser_artifacts(task_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_browser_artifacts_hash
    ON browser_artifacts(tenant_id, content_hash);

CREATE TABLE IF NOT EXISTS browser_parse_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    artifact_id UUID NOT NULL REFERENCES browser_artifacts(id) ON DELETE CASCADE,
    parser_id TEXT NOT NULL,
    parser_version TEXT NOT NULL DEFAULT '',
    normalized_rows JSONB NOT NULL DEFAULT '[]'::jsonb,
    validation_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'parsed',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_browser_parse_results_artifact
    ON browser_parse_results(artifact_id, created_at DESC);

CREATE TABLE IF NOT EXISTS browser_upload_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    task_id UUID NULL REFERENCES browser_tasks(id) ON DELETE SET NULL,
    recipe_run_id UUID NULL REFERENCES browser_recipe_runs(id) ON DELETE SET NULL,
    file_hash TEXT NOT NULL,
    target_url TEXT NOT NULL DEFAULT '',
    result_url TEXT NOT NULL DEFAULT '',
    receipt_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'submitted',
    verifier_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_browser_upload_results_task
    ON browser_upload_results(task_id, created_at DESC);
