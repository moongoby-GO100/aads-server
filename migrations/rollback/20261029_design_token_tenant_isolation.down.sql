-- Phase 1 rollback; incompatible data causes an atomic failure, never deletion.
BEGIN;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'design_projects'::regclass AND conname = 'design_projects_project_key_key') THEN
        ALTER TABLE design_projects ADD CONSTRAINT design_projects_project_key_key UNIQUE (project_key);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'design_token_sets'::regclass AND conname = 'design_token_sets_project_key_version_mode_key') THEN
        ALTER TABLE design_token_sets ADD CONSTRAINT design_token_sets_project_key_version_mode_key UNIQUE (project_key, version, mode);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'design_token_sets'::regclass AND conname = 'design_token_sets_project_key_fkey') THEN
        ALTER TABLE design_token_sets ADD CONSTRAINT design_token_sets_project_key_fkey FOREIGN KEY (project_key) REFERENCES design_projects(project_key) ON DELETE CASCADE;
    END IF;
END $$;
ALTER TABLE design_token_sets DROP CONSTRAINT IF EXISTS design_token_sets_tenant_project_fkey;
ALTER TABLE design_token_sets DROP CONSTRAINT IF EXISTS design_token_sets_tenant_project_version_mode_key;
ALTER TABLE design_token_sets DROP COLUMN IF EXISTS status;
ALTER TABLE design_token_sets DROP COLUMN IF EXISTS source;
ALTER TABLE design_token_sets DROP COLUMN IF EXISTS tenant_id;
ALTER TABLE design_projects DROP CONSTRAINT IF EXISTS design_projects_tenant_project_key;
ALTER TABLE design_projects DROP COLUMN IF EXISTS tenant_id;
COMMIT;
