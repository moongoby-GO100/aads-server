-- 100: SaaS multitenant foundation
-- - Create tenant, membership, and invite tables.
-- - Backfill existing single-operator data into the internal tenant.
-- - Attach core chat tables to tenant_id with FK/index coverage.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'customer'
                    CHECK (kind IN ('internal', 'customer')),
    status      TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'suspended', 'archived')),
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_by  TEXT REFERENCES saas_users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at  TIMESTAMPTZ
);

INSERT INTO tenants (slug, name, kind, status, metadata)
VALUES ('internal', 'AADS Internal', 'internal', 'active', '{"backfill":"single_operator_ceo"}'::jsonb)
ON CONFLICT (slug) DO UPDATE
   SET name = EXCLUDED.name,
       kind = EXCLUDED.kind,
       status = 'active',
       deleted_at = NULL,
       updated_at = NOW();

CREATE OR REPLACE FUNCTION public.aads_internal_tenant_id()
RETURNS UUID
LANGUAGE SQL
STABLE
AS $$
    SELECT id
      FROM public.tenants
     WHERE slug = 'internal'
       AND deleted_at IS NULL
     LIMIT 1
$$;

CREATE TABLE IF NOT EXISTS tenant_memberships (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'member'
                    CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    status      TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'invited', 'suspended', 'removed')),
    invited_by  TEXT REFERENCES saas_users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at  TIMESTAMPTZ,
    UNIQUE (tenant_id, user_id)
);

CREATE TABLE IF NOT EXISTS tenant_invites (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email        TEXT NOT NULL CHECK (btrim(email) <> ''),
    token_hash   TEXT NOT NULL UNIQUE,
    role         TEXT NOT NULL DEFAULT 'member'
                     CHECK (role IN ('admin', 'member', 'viewer')),
    status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'accepted', 'revoked', 'expired')),
    invited_by   TEXT REFERENCES saas_users(id) ON DELETE SET NULL,
    accepted_by  TEXT REFERENCES saas_users(id) ON DELETE SET NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    accepted_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tenants_status
    ON tenants(status)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tenant_memberships_user_active
    ON tenant_memberships(user_id, tenant_id)
    WHERE status = 'active' AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tenant_memberships_tenant_status
    ON tenant_memberships(tenant_id, status)
    WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_tenant_invites_pending_email
    ON tenant_invites(tenant_id, lower(email))
    WHERE status = 'pending' AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tenant_invites_tenant_status
    ON tenant_invites(tenant_id, status, expires_at)
    WHERE deleted_at IS NULL;

ALTER TABLE saas_users
    ADD COLUMN IF NOT EXISTS default_tenant_id UUID;
ALTER TABLE saas_users
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended', 'deleted'));
ALTER TABLE saas_users
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

UPDATE saas_users
   SET default_tenant_id = public.aads_internal_tenant_id()
 WHERE default_tenant_id IS NULL;

ALTER TABLE saas_users
    ALTER COLUMN default_tenant_id SET DEFAULT public.aads_internal_tenant_id();
ALTER TABLE saas_users
    ALTER COLUMN default_tenant_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_saas_users_default_tenant'
           AND conrelid = 'public.saas_users'::regclass
    ) THEN
        ALTER TABLE public.saas_users
            ADD CONSTRAINT fk_saas_users_default_tenant
            FOREIGN KEY (default_tenant_id) REFERENCES public.tenants(id);
    END IF;
END $$;

INSERT INTO tenant_memberships (tenant_id, user_id, role, status)
SELECT public.aads_internal_tenant_id(),
       id,
       CASE WHEN role IN ('ceo', 'admin', 'owner') THEN 'owner' ELSE 'member' END,
       'active'
  FROM saas_users
ON CONFLICT (tenant_id, user_id) DO UPDATE
   SET status = 'active',
       role = CASE
            WHEN tenant_memberships.role = 'owner' THEN 'owner'
            WHEN EXCLUDED.role = 'owner' THEN 'owner'
            ELSE tenant_memberships.role
       END,
       deleted_at = NULL,
       updated_at = NOW();

ALTER TABLE chat_workspaces
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE chat_sessions
    ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS tenant_id UUID;

UPDATE chat_workspaces
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;

UPDATE chat_sessions s
   SET tenant_id = COALESCE(w.tenant_id, public.aads_internal_tenant_id())
  FROM chat_workspaces w
 WHERE s.workspace_id = w.id
   AND s.tenant_id IS NULL;

UPDATE chat_sessions
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;

UPDATE chat_messages m
   SET tenant_id = COALESCE(s.tenant_id, public.aads_internal_tenant_id())
  FROM chat_sessions s
 WHERE m.session_id = s.id
   AND m.tenant_id IS NULL;

UPDATE chat_messages
   SET tenant_id = public.aads_internal_tenant_id()
 WHERE tenant_id IS NULL;

ALTER TABLE chat_workspaces
    ALTER COLUMN tenant_id SET DEFAULT public.aads_internal_tenant_id();
ALTER TABLE chat_workspaces
    ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE chat_sessions
    ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE chat_messages
    ALTER COLUMN tenant_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_chat_workspaces_tenant'
           AND conrelid = 'public.chat_workspaces'::regclass
    ) THEN
        ALTER TABLE public.chat_workspaces
            ADD CONSTRAINT fk_chat_workspaces_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ux_chat_workspaces_id_tenant'
           AND conrelid = 'public.chat_workspaces'::regclass
    ) THEN
        ALTER TABLE public.chat_workspaces
            ADD CONSTRAINT ux_chat_workspaces_id_tenant UNIQUE (id, tenant_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_chat_sessions_tenant'
           AND conrelid = 'public.chat_sessions'::regclass
    ) THEN
        ALTER TABLE public.chat_sessions
            ADD CONSTRAINT fk_chat_sessions_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_chat_sessions_workspace_tenant'
           AND conrelid = 'public.chat_sessions'::regclass
    ) THEN
        ALTER TABLE public.chat_sessions
            ADD CONSTRAINT fk_chat_sessions_workspace_tenant
            FOREIGN KEY (workspace_id, tenant_id)
            REFERENCES public.chat_workspaces(id, tenant_id)
            ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ux_chat_sessions_id_tenant'
           AND conrelid = 'public.chat_sessions'::regclass
    ) THEN
        ALTER TABLE public.chat_sessions
            ADD CONSTRAINT ux_chat_sessions_id_tenant UNIQUE (id, tenant_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_chat_messages_tenant'
           AND conrelid = 'public.chat_messages'::regclass
    ) THEN
        ALTER TABLE public.chat_messages
            ADD CONSTRAINT fk_chat_messages_tenant
            FOREIGN KEY (tenant_id) REFERENCES public.tenants(id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'fk_chat_messages_session_tenant'
           AND conrelid = 'public.chat_messages'::regclass
    ) THEN
        ALTER TABLE public.chat_messages
            ADD CONSTRAINT fk_chat_messages_session_tenant
            FOREIGN KEY (session_id, tenant_id)
            REFERENCES public.chat_sessions(id, tenant_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_saas_users_default_tenant
    ON saas_users(default_tenant_id)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_chat_workspaces_tenant_created
    ON chat_workspaces(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_tenant_workspace_updated
    ON chat_sessions(tenant_id, workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_messages_tenant_session_created
    ON chat_messages(tenant_id, session_id, created_at);

CREATE OR REPLACE FUNCTION public.aads_set_chat_session_tenant()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.tenant_id IS NULL THEN
        SELECT tenant_id
          INTO NEW.tenant_id
          FROM public.chat_workspaces
         WHERE id = NEW.workspace_id;
    END IF;
    IF NEW.tenant_id IS NULL THEN
        NEW.tenant_id := public.aads_internal_tenant_id();
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION public.aads_set_chat_message_tenant()
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
         WHERE tgname = 'trg_chat_sessions_set_tenant'
           AND tgrelid = 'public.chat_sessions'::regclass
    ) THEN
        CREATE TRIGGER trg_chat_sessions_set_tenant
        BEFORE INSERT OR UPDATE OF workspace_id, tenant_id
        ON public.chat_sessions
        FOR EACH ROW
        EXECUTE FUNCTION public.aads_set_chat_session_tenant();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'trg_chat_messages_set_tenant'
           AND tgrelid = 'public.chat_messages'::regclass
    ) THEN
        CREATE TRIGGER trg_chat_messages_set_tenant
        BEFORE INSERT OR UPDATE OF session_id, tenant_id
        ON public.chat_messages
        FOR EACH ROW
        EXECUTE FUNCTION public.aads_set_chat_message_tenant();
    END IF;
END $$;

COMMIT;
