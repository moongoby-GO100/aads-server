-- M7 canonical site knowledge.  This is additive and safe to apply repeatedly.
BEGIN;

DO $$
BEGIN
    IF to_regclass('public.authenticated_site_profiles') IS NULL
       OR to_regclass('public.browser_recipes') IS NULL
       OR to_regclass('public.browser_learned_artifact_versions') IS NULL
       OR to_regclass('public.ops_skill_versions') IS NULL
       OR to_regclass('public.memory_facts') IS NULL
       OR to_regclass('public.browser_live_facts') IS NULL
       OR to_regclass('public.browser_live_fact_events') IS NULL THEN
        RAISE EXCEPTION 'M7 canonical prerequisites are missing';
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

-- Redact legacy values in place.  The existing columns remain the stable API
-- contract, but never retain a source URL or variant source value after M7.
UPDATE browser_live_facts
   SET source_url = 'sha256:' || encode(digest(source_url, 'sha256'), 'hex')
 WHERE source_url !~ '^sha256:[0-9a-f]{64}$';
UPDATE browser_live_facts
   SET variant_key = 'sha256:' || encode(digest(variant_key, 'sha256'), 'hex')
 WHERE variant_key !~ '^sha256:[0-9a-f]{64}$';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='browser_live_facts_source_url_hash_check') THEN
        ALTER TABLE browser_live_facts ADD CONSTRAINT browser_live_facts_source_url_hash_check
            CHECK (source_url ~ '^sha256:[0-9a-f]{64}$');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='browser_live_facts_variant_key_hash_check') THEN
        ALTER TABLE browser_live_facts ADD CONSTRAINT browser_live_facts_variant_key_hash_check
            CHECK (variant_key ~ '^sha256:[0-9a-f]{64}$');
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_memory_facts_site_knowledge_scope
    ON memory_facts (tenant_id, site_profile_id, expires_at, updated_at DESC)
    WHERE tenant_id IS NOT NULL AND superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS idx_browser_recipes_site_knowledge_scope
    ON browser_recipes (tenant_id, site_profile_id, expires_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_browser_live_facts_site_knowledge_scope
    ON browser_live_facts (tenant_id, site_profile_id, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_skill_versions_site_scope
    ON ops_skill_versions (site_profile_id, status, expires_at);

COMMIT;
