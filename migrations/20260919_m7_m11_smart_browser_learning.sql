-- M7-M11: tenant/site/version-scoped Smart Browser learning and retrieval.
-- Prerequisites are additive G3/G5/G6 migrations from the same release chain.
BEGIN;

DO $$
BEGIN
    IF to_regclass('public.browser_learned_artifact_versions') IS NULL
       OR to_regclass('public.browser_live_facts') IS NULL
       OR to_regclass('public.ops_skill_versions') IS NULL THEN
        RAISE EXCEPTION 'smart browser prerequisites missing: apply G3, G5, and G6 first';
    END IF;
END $$;

ALTER TABLE authenticated_site_profiles
    ADD COLUMN IF NOT EXISTS knowledge_version BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS knowledge_provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS knowledge_expires_at TIMESTAMPTZ;

ALTER TABLE browser_recipes
    ADD COLUMN IF NOT EXISTS site_profile_id UUID REFERENCES authenticated_site_profiles(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

ALTER TABLE browser_learned_artifact_versions
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

ALTER TABLE ops_skill_versions
    ADD COLUMN IF NOT EXISTS site_profile_id UUID REFERENCES authenticated_site_profiles(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

ALTER TABLE memory_facts
    ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS site_profile_id UUID REFERENCES authenticated_site_profiles(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

ALTER TABLE browser_live_facts
    ADD COLUMN IF NOT EXISTS site_profile_id UUID REFERENCES authenticated_site_profiles(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_memory_facts_site_knowledge_scope
    ON memory_facts (tenant_id, site_profile_id, expires_at, updated_at DESC)
    WHERE tenant_id IS NOT NULL AND superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_browser_recipes_site_knowledge_scope
    ON browser_recipes (tenant_id, site_profile_id, expires_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_browser_live_facts_site_knowledge_scope
    ON browser_live_facts (tenant_id, site_profile_id, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_skill_versions_site_scope
    ON ops_skill_versions (site_profile_id, status, expires_at);

CREATE TABLE IF NOT EXISTS browser_site_runtime_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_profile_id UUID NOT NULL REFERENCES authenticated_site_profiles(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('initial_learning','revisit_assessment','skill_resolution','readonly_e2e')),
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_browser_site_runtime_events_scope
    ON browser_site_runtime_events (tenant_id, site_profile_id, created_at DESC);

CREATE TABLE IF NOT EXISTS browser_site_skill_embeddings (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    site_profile_id UUID NOT NULL REFERENCES authenticated_site_profiles(id) ON DELETE CASCADE,
    skill_version_id UUID NOT NULL REFERENCES ops_skill_versions(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    instruction_version TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(skill_version_id,model_id,instruction_version)
);
CREATE INDEX IF NOT EXISTS idx_browser_site_skill_embeddings_scope
    ON browser_site_skill_embeddings (tenant_id,site_profile_id,model_id,instruction_version);

CREATE OR REPLACE FUNCTION enforce_authenticated_site_profile_origin()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    item TEXT;
    normalized TEXT;
BEGIN
    normalized := lower((regexp_match(NEW.base_origin, '^(https?://[^/]+)'))[1]);
    IF normalized !~ '^https?://[^/@:]+(:[0-9]+)?$' THEN
        RAISE EXCEPTION 'site profile base_origin must be a credential-free http(s) origin';
    END IF;
    NEW.base_origin := normalized;
    IF jsonb_typeof(NEW.allowed_origins) <> 'array' OR jsonb_array_length(NEW.allowed_origins) = 0 THEN
        RAISE EXCEPTION 'site profile allowed_origins must be a nonempty array';
    END IF;
    FOR item IN SELECT jsonb_array_elements_text(NEW.allowed_origins) LOOP
        normalized := lower((regexp_match(item, '^(https?://[^/]+)'))[1]);
        IF normalized !~ '^https?://[^/@:]+(:[0-9]+)?$' THEN
            RAISE EXCEPTION 'site profile allowed origin must be credential-free http(s) origin';
        END IF;
    END LOOP;
    NEW.allowed_origins := (
        SELECT jsonb_agg(origin ORDER BY origin)
          FROM (
            SELECT DISTINCT lower((regexp_match(value, '^(https?://[^/]+)'))[1]) AS origin
              FROM jsonb_array_elements_text(NEW.allowed_origins) value
          ) normalized_origins
    );
    IF NOT NEW.allowed_origins ? NEW.base_origin THEN
        NEW.allowed_origins := NEW.allowed_origins || jsonb_build_array(NEW.base_origin);
    END IF;
    NEW.knowledge_version := CASE
        WHEN TG_OP = 'INSERT' THEN NEW.knowledge_version
        ELSE OLD.knowledge_version + 1
    END;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname='trg_authenticated_site_profile_origin'
           AND tgrelid='authenticated_site_profiles'::regclass
           AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_authenticated_site_profile_origin
        BEFORE INSERT OR UPDATE OF base_origin,allowed_origins
        ON authenticated_site_profiles
        FOR EACH ROW EXECUTE FUNCTION enforce_authenticated_site_profile_origin();
    END IF;
END $$;

COMMIT;
