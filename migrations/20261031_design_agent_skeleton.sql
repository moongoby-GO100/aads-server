-- AADS-DESIGN-AGENT-D1-SKELETON-20261029: after project backfill.
BEGIN;
CREATE TABLE IF NOT EXISTS design_agent_triggers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    project_key TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('project_created','deploy_succeeded','manual','screen_change_requested','new_screen_requested')),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','claimed','done','failed','skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    UNIQUE (tenant_id, dedupe_key)
);
CREATE TABLE IF NOT EXISTS design_agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_id UUID NOT NULL REFERENCES design_agent_triggers(id),
    tenant_id UUID NOT NULL,
    project_key TEXT NOT NULL,
    handler TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','skipped_duplicate','blocked_budget','blocked_disabled')),
    llm_calls INTEGER NOT NULL DEFAULT 0 CHECK (llm_calls BETWEEN 0 AND 15),
    cost_usd NUMERIC(10,4) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    writes JSONB NOT NULL DEFAULT '[]'::jsonb,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_design_agent_runs_tenant_started
    ON design_agent_runs (tenant_id, started_at);
CREATE INDEX IF NOT EXISTS idx_design_agent_triggers_queue
    ON design_agent_triggers (tenant_id, created_at, id) WHERE status='queued';
COMMIT;
