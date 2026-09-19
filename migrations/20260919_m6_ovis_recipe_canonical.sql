-- M6: canonical OVISRecipe references.  Legacy tables remain compatibility adapters.
-- This migration is additive and lossless: source rows are never updated or deleted.
BEGIN;

CREATE TABLE IF NOT EXISTS ovis_recipes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NULL REFERENCES tenants(id) ON DELETE CASCADE,
    canonical_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, canonical_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ovis_recipes_scope
    ON ovis_recipes (COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), canonical_key);

CREATE TABLE IF NOT EXISTS ovis_recipe_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipe_id UUID NOT NULL REFERENCES ovis_recipes(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','candidate','shadow','active','archived','deprecated','quarantined')),
    definition JSONB NOT NULL,
    definition_sha256 TEXT NOT NULL,
    approval_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    skill_version_id UUID NULL REFERENCES ops_skill_versions(id) ON DELETE SET NULL,
    promotion_artifact_id UUID NULL REFERENCES browser_learned_artifacts(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (recipe_id, version)
);
CREATE INDEX IF NOT EXISTS idx_ovis_recipe_versions_scope
    ON ovis_recipe_versions (recipe_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS ovis_recipe_legacy_refs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ovis_recipe_version_id UUID NOT NULL REFERENCES ovis_recipe_versions(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN ('work_recipe','browser_recipe','site_skill')),
    source_id TEXT NOT NULL,
    rollback_definition JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_type, source_id)
);

-- BrowserRecipe: preserve complete rows, tenant/version/status, and the legacy id.
INSERT INTO ovis_recipes (tenant_id, canonical_key)
SELECT DISTINCT tenant_id, 'browser:' || recipe_id FROM browser_recipes
ON CONFLICT (tenant_id, canonical_key) DO NOTHING;
INSERT INTO ovis_recipe_versions (recipe_id, version, status, definition, definition_sha256, approval_scope)
SELECT o.id, b.version || '@' || left(b.version_hash, 16),
       CASE WHEN b.version_status IN ('draft','active','archived') THEN b.version_status ELSE 'draft' END,
       to_jsonb(b), 'sha256:' || encode(digest(convert_to(to_jsonb(b)::text, 'UTF8'), 'sha256'), 'hex'),
       jsonb_build_object('legacy','browser_recipe','tenant_id',b.tenant_id,'version',b.version)
  FROM browser_recipes b JOIN ovis_recipes o ON o.tenant_id=b.tenant_id AND o.canonical_key='browser:' || b.recipe_id
ON CONFLICT (recipe_id, version) DO NOTHING;
INSERT INTO ovis_recipe_legacy_refs (ovis_recipe_version_id, source_type, source_id, rollback_definition)
SELECT v.id, 'browser_recipe', b.id::text, to_jsonb(b)
  FROM browser_recipes b JOIN ovis_recipes o ON o.tenant_id=b.tenant_id AND o.canonical_key='browser:' || b.recipe_id
  JOIN ovis_recipe_versions v ON v.recipe_id=o.id AND v.version=b.version || '@' || left(b.version_hash, 16)
ON CONFLICT (source_type, source_id) DO NOTHING;

-- WorkRecipe global rows retain their NULL scope; the expression index above
-- keeps them in one version namespace without manufacturing a tenant.
INSERT INTO ovis_recipes (tenant_id, canonical_key)
SELECT DISTINCT tenant_id, 'work:' || domain || ':' || name FROM work_recipes
ON CONFLICT DO NOTHING;
INSERT INTO ovis_recipe_versions (recipe_id, version, status, definition, definition_sha256, approval_scope)
SELECT o.id, w.version::text, CASE WHEN w.enabled THEN 'active' ELSE 'archived' END,
       to_jsonb(w), 'sha256:' || encode(digest(convert_to(to_jsonb(w)::text, 'UTF8'), 'sha256'), 'hex'),
       jsonb_build_object('legacy','work_recipe','tenant_id',w.tenant_id,'max_risk',w.max_risk)
  FROM work_recipes w JOIN ovis_recipes o ON o.tenant_id IS NOT DISTINCT FROM w.tenant_id AND o.canonical_key='work:' || w.domain || ':' || w.name
ON CONFLICT (recipe_id, version) DO NOTHING;
INSERT INTO ovis_recipe_legacy_refs (ovis_recipe_version_id, source_type, source_id, rollback_definition)
SELECT v.id, 'work_recipe', w.id::text, to_jsonb(w)
  FROM work_recipes w JOIN ovis_recipes o ON o.tenant_id IS NOT DISTINCT FROM w.tenant_id AND o.canonical_key='work:' || w.domain || ':' || w.name
  JOIN ovis_recipe_versions v ON v.recipe_id=o.id AND v.version=w.version::text
ON CONFLICT (source_type, source_id) DO NOTHING;

-- G3 remains owner of executable contracts; this reference merely binds it to OVISRecipe.
INSERT INTO ovis_recipes (tenant_id, canonical_key)
SELECT l.tenant_id, 'skill:' || l.slug FROM ops_skill_library l
ON CONFLICT (tenant_id, canonical_key) DO NOTHING;
INSERT INTO ovis_recipe_versions (recipe_id, version, status, definition, definition_sha256, approval_scope, skill_version_id)
SELECT o.id, s.version, s.status, s.manifest,
       s.content_sha256, jsonb_build_object('legacy','site_skill','tenant_id',l.tenant_id), s.id
  FROM ops_skill_library l JOIN ops_skill_versions s ON s.skill_id=l.id
  JOIN ovis_recipes o ON o.tenant_id=l.tenant_id AND o.canonical_key='skill:' || l.slug
ON CONFLICT (recipe_id, version) DO NOTHING;
INSERT INTO ovis_recipe_legacy_refs (ovis_recipe_version_id, source_type, source_id, rollback_definition)
SELECT v.id, 'site_skill', s.id::text, s.manifest
  FROM ops_skill_library l JOIN ops_skill_versions s ON s.skill_id=l.id
  JOIN ovis_recipes o ON o.tenant_id=l.tenant_id AND o.canonical_key='skill:' || l.slug
  JOIN ovis_recipe_versions v ON v.recipe_id=o.id AND v.version=s.version
ON CONFLICT (source_type, source_id) DO NOTHING;

-- Rollback is reference-only: legacy source rows are untouched and each exact
-- pre-canonical payload is retained in rollback_definition.
COMMIT;
