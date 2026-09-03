-- AADS-LOGIN-COLLECTOR-SAAS-MVP
-- Product-facing authenticated site collector control-plane tables.
-- Additive only. Credentials, cookies and challenge answers are intentionally excluded.

CREATE TABLE IF NOT EXISTS authenticated_site_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_key TEXT NOT NULL,
    site_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    base_origin TEXT NOT NULL,
    allowed_origins JSONB NOT NULL DEFAULT '[]'::jsonb,
    runtime TEXT NOT NULL,
    data_categories JSONB NOT NULL DEFAULT '[]'::jsonb,
    login_mode TEXT NOT NULL DEFAULT 'user_session',
    challenge_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    retention_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, project_key, site_key),
    CHECK (project_key IN ('AADS','KIS','GO100','SF','NTV2','NAS','STORE_ASSISTANT','MARKETING','BANKING','CUSTOM')),
    CHECK (runtime IN ('webview2','windows_collector','chrome_extension','chrome_cdp','playwright_server','file_upload','official_api','manual_export'))
);

CREATE INDEX IF NOT EXISTS idx_authenticated_site_profiles_scope
    ON authenticated_site_profiles(tenant_id, project_key, enabled, updated_at DESC);

CREATE TABLE IF NOT EXISTS authenticated_site_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_profile_id UUID NOT NULL REFERENCES authenticated_site_profiles(id) ON DELETE CASCADE,
    account_label TEXT NOT NULL,
    vault_reference TEXT NOT NULL DEFAULT '',
    login_status TEXT NOT NULL DEFAULT 'login_required',
    last_authenticated_at TIMESTAMPTZ NULL,
    last_collected_at TIMESTAMPTZ NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, site_profile_id, account_label),
    CHECK (login_status IN ('connected','login_required','action_required','expired','disabled'))
);

ALTER TABLE browser_recipes ADD COLUMN IF NOT EXISTS project_key TEXT NOT NULL DEFAULT 'CUSTOM';
ALTER TABLE browser_recipes ADD COLUMN IF NOT EXISTS site_environment TEXT NOT NULL DEFAULT 'chrome_cdp';
ALTER TABLE browser_recipes ADD COLUMN IF NOT EXISTS record_types JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE browser_recipes ADD COLUMN IF NOT EXISTS normalization_schema JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE browser_recipes ADD COLUMN IF NOT EXISTS fixture_cases JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE browser_recipes ADD COLUMN IF NOT EXISTS version_status TEXT NOT NULL DEFAULT 'draft';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'browser_recipes_project_key_check'
    ) THEN
        ALTER TABLE browser_recipes ADD CONSTRAINT browser_recipes_project_key_check
            CHECK (project_key IN ('AADS','KIS','GO100','SF','NTV2','NAS','STORE_ASSISTANT','MARKETING','BANKING','CUSTOM'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'browser_recipes_site_environment_check'
    ) THEN
        ALTER TABLE browser_recipes ADD CONSTRAINT browser_recipes_site_environment_check
            CHECK (site_environment IN ('webview2','windows_collector','chrome_extension','chrome_cdp','playwright_server','file_upload','official_api','manual_export'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'browser_recipes_version_status_check'
    ) THEN
        ALTER TABLE browser_recipes ADD CONSTRAINT browser_recipes_version_status_check
            CHECK (version_status IN ('draft','active','archived'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_browser_recipes_saas_scope
    ON browser_recipes(tenant_id, project_key, service, version_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS authenticated_collector_audit_log (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    actor_user_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL DEFAULT '',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_authenticated_collector_audit_scope
    ON authenticated_collector_audit_log(tenant_id, created_at DESC);
