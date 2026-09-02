-- 144: User-owned project server registry
-- Stores only server metadata and ownership. SSH secrets must stay in the
-- user's PC Agent or Agent Vault, not in this table.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS user_project_servers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL REFERENCES saas_users(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    workspace_id UUID REFERENCES chat_workspaces(id) ON DELETE SET NULL,
    project_key TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL,
    ssh_user TEXT NOT NULL DEFAULT 'partner',
    ssh_port INTEGER NOT NULL DEFAULT 22 CHECK (ssh_port BETWEEN 1 AND 65535),
    auth_type TEXT NOT NULL DEFAULT 'ssh_key'
        CHECK (auth_type IN ('ssh_key', 'agent_vault', 'manual')),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled', 'archived')),
    connection_state TEXT NOT NULL DEFAULT 'unverified'
        CHECK (connection_state IN ('unverified', 'reachable', 'unreachable', 'auth_failed')),
    last_checked_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_user_project_servers_no_secret_metadata
        CHECK (
            NOT (metadata ? 'password')
            AND NOT (metadata ? 'private_key')
            AND NOT (metadata ? 'api_key')
            AND NOT (metadata ? 'secret')
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_user_project_servers_owner_target
    ON user_project_servers(user_id, tenant_id, host, ssh_port, ssh_user)
    WHERE status <> 'archived';

CREATE INDEX IF NOT EXISTS idx_user_project_servers_owner
    ON user_project_servers(user_id, tenant_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_user_project_servers_workspace
    ON user_project_servers(workspace_id)
    WHERE workspace_id IS NOT NULL;

COMMIT;
