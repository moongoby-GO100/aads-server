-- Approval-scoped automation tokens for managed browser risky actions.
-- Non-destructive: adds columns/table only.

ALTER TABLE agent_permission_requests
    ADD COLUMN IF NOT EXISTS approval_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS max_executions INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS token_hash TEXT NULL;

CREATE TABLE IF NOT EXISTS browser_approval_tokens (
    token_hash TEXT PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES browser_tasks(id) ON DELETE CASCADE,
    request_id UUID NOT NULL REFERENCES agent_permission_requests(id) ON DELETE CASCADE,
    work_key TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL,
    action_summary TEXT NOT NULL DEFAULT '',
    approval_scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_executions INTEGER NOT NULL DEFAULT 1,
    used_executions INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    revoked_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_browser_approval_tokens_task
    ON browser_approval_tokens(tenant_id, task_id, expires_at DESC);

CREATE INDEX IF NOT EXISTS idx_browser_approval_tokens_active
    ON browser_approval_tokens(tenant_id, revoked_at, expires_at)
    WHERE revoked_at IS NULL;
