-- AADS-PC-AGENT-GLOBAL-COLLECTION-QUEUE-P0
-- Global admission queue for one-PC authenticated bank/sales-site collection.
-- Additive only. No destructive statements.

CREATE TABLE IF NOT EXISTS pc_agent_collection_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NULL REFERENCES tenants(id) ON DELETE CASCADE,
    job_key TEXT NOT NULL UNIQUE,
    queue_type TEXT NOT NULL DEFAULT 'delivery',
    site_key TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT '',
    business_id TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    work_key TEXT NOT NULL DEFAULT '',
    resource_key TEXT NOT NULL DEFAULT '',
    runtime TEXT NOT NULL DEFAULT 'pc_agent',
    priority INTEGER NOT NULL DEFAULT 50,
    min_interval_seconds INTEGER NOT NULL DEFAULT 900,
    latest_only BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL DEFAULT 'queued',
    next_run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_agent_id TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NULL,
    finished_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (queue_type IN ('delivery','bank','financial','browser_recipe')),
    CHECK (status IN ('queued','running','succeeded','failed','action_required','superseded','cancelled')),
    CHECK (priority >= 0),
    CHECK (min_interval_seconds >= 0),
    CHECK (max_attempts >= 1)
);

CREATE INDEX IF NOT EXISTS idx_pc_agent_collection_queue_due
    ON pc_agent_collection_queue(status, next_run_at, priority, created_at)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_pc_agent_collection_queue_resource_active
    ON pc_agent_collection_queue(resource_key, status, updated_at DESC)
    WHERE status IN ('queued','running','action_required');

CREATE INDEX IF NOT EXISTS idx_pc_agent_collection_queue_scope
    ON pc_agent_collection_queue(service, business_id, branch, updated_at DESC);
