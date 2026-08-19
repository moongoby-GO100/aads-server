-- AADS-186: OHVIS Managed Browser + Agent Vault P0
-- Idempotent additive schema only. No destructive statements.

CREATE TABLE IF NOT EXISTS agent_vault_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    work_key TEXT NOT NULL,
    origin TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT 'default',
    username_enc TEXT NOT NULL,
    password_enc TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by TEXT NOT NULL DEFAULT '',
    last_used_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, work_key, origin, label)
);

CREATE INDEX IF NOT EXISTS idx_agent_vault_credentials_tenant_work
    ON agent_vault_credentials(tenant_id, work_key, is_active);

CREATE TABLE IF NOT EXISTS agent_vault_autofill_tokens (
    token_hash TEXT PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    credential_id UUID NOT NULL REFERENCES agent_vault_credentials(id) ON DELETE CASCADE,
    work_key TEXT NOT NULL,
    origin TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    redeemed_at TIMESTAMPTZ NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_vault_autofill_tokens_expiry
    ON agent_vault_autofill_tokens(expires_at, redeemed_at);

CREATE TABLE IF NOT EXISTS agent_vault_access_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    credential_id UUID NULL,
    work_key TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    actor_user_id TEXT NOT NULL DEFAULT '',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_vault_access_logs_tenant_created
    ON agent_vault_access_logs(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_permission_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    task_id UUID NULL,
    work_key TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL,
    action_summary TEXT NOT NULL DEFAULT '',
    risk_level TEXT NOT NULL DEFAULT 'medium',
    decision TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    decided_by TEXT NULL,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '10 minutes'),
    decided_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_permission_requests_pending
    ON agent_permission_requests(tenant_id, decision, expires_at);

CREATE TABLE IF NOT EXISTS browser_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    session_id UUID NULL,
    work_key TEXT NOT NULL,
    target_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    current_step TEXT NOT NULL DEFAULT '',
    requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
    approval_request_id UUID NULL REFERENCES agent_permission_requests(id) ON DELETE SET NULL,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_browser_tasks_tenant_status
    ON browser_tasks(tenant_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS browser_task_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES browser_tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_browser_task_events_task_created
    ON browser_task_events(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS browser_routines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    work_key TEXT NOT NULL,
    name TEXT NOT NULL,
    target_url TEXT NOT NULL,
    steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, work_key, name)
);
