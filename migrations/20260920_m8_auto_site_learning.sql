-- M8: serialize first-visit learning and monotonic candidate allocation per tenant/site/page.
-- The canonical Page Template and Site Skill payloads remain in the G6/G3 tables.
BEGIN;

DO $$
BEGIN
    IF to_regclass('public.browser_learned_artifacts') IS NULL
       OR to_regclass('public.ops_skill_library') IS NULL
       OR to_regclass('public.ops_skill_versions') IS NULL
       OR to_regclass('public.authenticated_site_profiles') IS NULL THEN
        RAISE EXCEPTION 'M8 auto-learning prerequisites are missing';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS browser_site_learning_scopes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_profile_id UUID NOT NULL REFERENCES authenticated_site_profiles(id) ON DELETE CASCADE,
    page_key TEXT NOT NULL,
    page_artifact_id UUID REFERENCES browser_learned_artifacts(id) ON DELETE RESTRICT,
    skill_id UUID REFERENCES ops_skill_library(id) ON DELETE RESTRICT,
    next_version BIGINT NOT NULL DEFAULT 1 CHECK (next_version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, site_profile_id, page_key),
    UNIQUE (tenant_id, page_artifact_id),
    UNIQUE (tenant_id, skill_id)
);

CREATE INDEX IF NOT EXISTS idx_browser_site_learning_scope_lookup
    ON browser_site_learning_scopes (tenant_id, site_profile_id, page_key);

CREATE OR REPLACE FUNCTION enforce_browser_site_learning_scope_tenant()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM authenticated_site_profiles
         WHERE id=NEW.site_profile_id AND tenant_id=NEW.tenant_id
    ) THEN
        RAISE EXCEPTION 'site learning profile tenant mismatch';
    END IF;
    IF NEW.page_artifact_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM browser_learned_artifacts
         WHERE id=NEW.page_artifact_id AND tenant_id=NEW.tenant_id
    ) THEN
        RAISE EXCEPTION 'site learning artifact tenant mismatch';
    END IF;
    IF NEW.skill_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM ops_skill_library
         WHERE id=NEW.skill_id AND tenant_id=NEW.tenant_id
    ) THEN
        RAISE EXCEPTION 'site learning skill tenant mismatch';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname='trg_browser_site_learning_scope_tenant'
           AND tgrelid='browser_site_learning_scopes'::regclass
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_browser_site_learning_scope_tenant
        BEFORE INSERT OR UPDATE ON browser_site_learning_scopes
        FOR EACH ROW EXECUTE FUNCTION enforce_browser_site_learning_scope_tenant();
    END IF;
END $$;

COMMIT;
