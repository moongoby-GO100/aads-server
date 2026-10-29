-- Phase 1: retain design_projects UNIQUE(project_key) for the other four FKs.
-- Cross-tenant duplicate project keys require Phase 2 migration of those FKs.
-- The composite token FK also prevents using the same project_key in two tenants
-- until Phase 2; only token uniqueness itself is tenant-scoped in Phase 1.
BEGIN;
ALTER TABLE design_projects ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE design_projects SET tenant_id = '2d701a8c-9596-4757-8588-faa4f7837112'
WHERE tenant_id IS NULL AND project_key = 'AADS';
-- Unknown legacy projects fail closed at NOT NULL rather than guessing ownership.
ALTER TABLE design_projects ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE design_token_sets ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE design_token_sets t SET tenant_id = p.tenant_id
FROM design_projects p WHERE t.project_key = p.project_key AND t.tenant_id IS NULL;
ALTER TABLE design_token_sets ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE design_token_sets ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE design_token_sets ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'design_projects'::regclass AND conname = 'design_projects_tenant_project_key') THEN
        ALTER TABLE design_projects ADD CONSTRAINT design_projects_tenant_project_key UNIQUE (tenant_id, project_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'design_token_sets'::regclass AND conname = 'design_token_sets_status_check') THEN
        ALTER TABLE design_token_sets ADD CONSTRAINT design_token_sets_status_check CHECK (status IN ('draft','approved','superseded','archived'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'design_token_sets'::regclass AND conname = 'design_token_sets_source_check') THEN
        ALTER TABLE design_token_sets ADD CONSTRAINT design_token_sets_source_check CHECK (source IN ('manual','extracted','imported_genspark','generated'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'design_token_sets'::regclass AND conname = 'design_token_sets_tenant_project_version_mode_key') THEN
        ALTER TABLE design_token_sets ADD CONSTRAINT design_token_sets_tenant_project_version_mode_key UNIQUE (tenant_id, project_key, version, mode);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'design_token_sets'::regclass AND conname = 'design_token_sets_tenant_project_fkey') THEN
        ALTER TABLE design_token_sets ADD CONSTRAINT design_token_sets_tenant_project_fkey FOREIGN KEY (tenant_id, project_key) REFERENCES design_projects(tenant_id, project_key) ON DELETE CASCADE;
    END IF;
END $$;
ALTER TABLE design_token_sets DROP CONSTRAINT IF EXISTS design_token_sets_project_key_fkey;
ALTER TABLE design_token_sets DROP CONSTRAINT IF EXISTS design_token_sets_project_key_version_mode_key;
COMMIT;
