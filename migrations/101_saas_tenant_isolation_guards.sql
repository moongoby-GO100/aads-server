-- 101: SaaS tenant isolation guards for high-risk data tables.

BEGIN;

ALTER TABLE chat_artifacts
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE e2e_credentials
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE project_artifacts
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE directive_lifecycle
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

UPDATE chat_artifacts a
   SET tenant_id = COALESCE(s.tenant_id, public.aads_internal_tenant_id())
  FROM chat_sessions s
 WHERE a.session_id = s.id
   AND a.tenant_id IS NULL;

UPDATE chat_artifacts
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;

UPDATE e2e_credentials
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;

UPDATE project_artifacts
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;

UPDATE pipeline_jobs pj
   SET tenant_id = COALESCE(s.tenant_id, public.aads_internal_tenant_id())
  FROM chat_sessions s
 WHERE pj.chat_session_id = s.id
   AND pj.tenant_id IS NULL;

UPDATE pipeline_jobs
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;

UPDATE directive_lifecycle
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;

ALTER TABLE chat_artifacts
    ALTER COLUMN tenant_id SET DEFAULT public.aads_internal_tenant_id();
ALTER TABLE e2e_credentials
    ALTER COLUMN tenant_id SET DEFAULT public.aads_internal_tenant_id();
ALTER TABLE project_artifacts
    ALTER COLUMN tenant_id SET DEFAULT public.aads_internal_tenant_id();
ALTER TABLE pipeline_jobs
    ALTER COLUMN tenant_id SET DEFAULT public.aads_internal_tenant_id();
ALTER TABLE directive_lifecycle
    ALTER COLUMN tenant_id SET DEFAULT public.aads_internal_tenant_id();

ALTER TABLE chat_artifacts
    ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE e2e_credentials
    ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE project_artifacts
    ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE pipeline_jobs
    ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE directive_lifecycle
    ALTER COLUMN tenant_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'uq_chat_sessions_id_tenant'
           AND conrelid = 'public.chat_sessions'::regclass
    ) THEN
        ALTER TABLE public.chat_sessions
            ADD CONSTRAINT uq_chat_sessions_id_tenant UNIQUE (id, tenant_id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_chat_artifacts_tenant'
           AND conrelid = 'public.chat_artifacts'::regclass
    ) THEN
        ALTER TABLE public.chat_artifacts
            ADD CONSTRAINT fk_chat_artifacts_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_chat_artifacts_session_tenant'
           AND conrelid = 'public.chat_artifacts'::regclass
    ) THEN
        ALTER TABLE public.chat_artifacts
            ADD CONSTRAINT fk_chat_artifacts_session_tenant
            FOREIGN KEY (session_id, tenant_id)
            REFERENCES public.chat_sessions(id, tenant_id)
            ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_e2e_credentials_tenant'
           AND conrelid = 'public.e2e_credentials'::regclass
    ) THEN
        ALTER TABLE public.e2e_credentials
            ADD CONSTRAINT fk_e2e_credentials_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_project_artifacts_tenant'
           AND conrelid = 'public.project_artifacts'::regclass
    ) THEN
        ALTER TABLE public.project_artifacts
            ADD CONSTRAINT fk_project_artifacts_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_pipeline_jobs_tenant'
           AND conrelid = 'public.pipeline_jobs'::regclass
    ) THEN
        ALTER TABLE public.pipeline_jobs
            ADD CONSTRAINT fk_pipeline_jobs_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_directive_lifecycle_tenant'
           AND conrelid = 'public.directive_lifecycle'::regclass
    ) THEN
        ALTER TABLE public.directive_lifecycle
            ADD CONSTRAINT fk_directive_lifecycle_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;
END $$;

DROP INDEX IF EXISTS idx_e2e_cred_service_project_label;

CREATE UNIQUE INDEX IF NOT EXISTS idx_e2e_cred_tenant_service_project_label
    ON e2e_credentials (tenant_id, service, COALESCE(project, '_ALL_'), label);
CREATE INDEX IF NOT EXISTS idx_e2e_cred_tenant_project
    ON e2e_credentials (tenant_id, project)
    WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_chat_artifacts_tenant_session
    ON chat_artifacts (tenant_id, session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_project_artifacts_tenant_project
    ON project_artifacts (tenant_id, project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_tenant_status
    ON pipeline_jobs (tenant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_directive_lifecycle_tenant_status
    ON directive_lifecycle (tenant_id, status);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'chat_artifacts'
           AND column_name = 'workspace_id'
    ) THEN
        CREATE INDEX IF NOT EXISTS idx_chat_artifacts_tenant_workspace
            ON chat_artifacts (tenant_id, workspace_id, created_at DESC)
            WHERE workspace_id IS NOT NULL;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.aads_set_chat_artifact_tenant()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.tenant_id IS NULL THEN
        SELECT tenant_id
          INTO NEW.tenant_id
          FROM public.chat_sessions
         WHERE id = NEW.session_id;
    END IF;
    IF NEW.tenant_id IS NULL THEN
        NEW.tenant_id := public.aads_internal_tenant_id();
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'trg_chat_artifacts_set_tenant'
           AND tgrelid = 'public.chat_artifacts'::regclass
    ) THEN
        CREATE TRIGGER trg_chat_artifacts_set_tenant
        BEFORE INSERT OR UPDATE OF session_id, tenant_id
        ON public.chat_artifacts
        FOR EACH ROW
        EXECUTE FUNCTION public.aads_set_chat_artifact_tenant();
    END IF;
END $$;

COMMIT;
