-- AADS-LOGIN-COLLECTOR-SAAS-MVP
-- Product-facing authenticated site collector control-plane tables.
-- Additive only. No destructive statements.

CREATE TABLE IF NOT EXISTS authenticated_site_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_key TEXT NOT NULL DEFAULT 'CUSTOM',
    site_key TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    base_origin TEXT NOT NULL DEFAULT '',
    allowed_origins JSONB NOT NULL DEFAULT '[]'::jsonb,
    runtime TEXT NOT NULL DEFAULT 'webview2',
    data_categories JSONB NOT NULL DEFAULT '[]'::jsonb,
    login_mode TEXT NOT NULL DEFAULT 'user_session',
    challenge_policy TEXT NOT NULL DEFAULT 'user_intervention',
    retention_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    account_count INTEGER NOT NULL DEFAULT 0,
    connected_account_count INTEGER NOT NULL DEFAULT 0,
    last_collected_at TIMESTAMPTZ NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, project_key, site_key),
    CHECK (project_key IN ('AADS','KIS','GO100','SF','NTV2','NAS','CUSTOM')),
    CHECK (runtime IN ('webview2','chrome_extension','chrome_cdp','playwright_server','file_upload','official_api','manual_export')),
    CHECK (login_mode IN ('user_session','agent_vault','manual_export','official_api','none')),
    CHECK (challenge_policy IN ('user_intervention','manual_export','deny','none')),
    CHECK (account_count >= 0),
    CHECK (connected_account_count >= 0)
);

CREATE INDEX IF NOT EXISTS idx_authenticated_site_profiles_tenant_project
    ON authenticated_site_profiles(tenant_id, project_key, enabled);

CREATE INDEX IF NOT EXISTS idx_authenticated_site_profiles_site
    ON authenticated_site_profiles(site_key, runtime);

ALTER TABLE browser_recipes
    ADD COLUMN IF NOT EXISTS project_key TEXT NOT NULL DEFAULT 'CUSTOM',
    ADD COLUMN IF NOT EXISTS site_environment TEXT NOT NULL DEFAULT 'webview2',
    ADD COLUMN IF NOT EXISTS record_types JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS normalization_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS fixture_cases JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS version_status TEXT NOT NULL DEFAULT 'draft';

CREATE INDEX IF NOT EXISTS idx_browser_recipes_project_status
    ON browser_recipes(tenant_id, project_key, service, version_status, enabled);
