-- G5: facts shown to users must be revalidated at the final display boundary.
-- Included in the M7-M11 release chain so a previously skipped G5 asset is
-- applied before site-knowledge columns are added.
CREATE TABLE IF NOT EXISTS browser_live_facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    session_id UUID REFERENCES chat_sessions(id) ON DELETE SET NULL,
    task_id UUID REFERENCES browser_tasks(id) ON DELETE SET NULL,
    fact_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    variant_key TEXT NOT NULL DEFAULT '',
    account_context_hash TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    revalidator_key TEXT NOT NULL,
    observed_value JSONB NOT NULL,
    observed_value_hash TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revalidated_at TIMESTAMPTZ NOT NULL,
    freshness_status TEXT NOT NULL CHECK (freshness_status IN ('CURRENT','STALE','UNAVAILABLE','CONFLICT')),
    evidence_id TEXT,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    version BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at > observed_at),
    CHECK (account_context_hash = '' OR account_context_hash ~ '^[0-9a-f]{64}$'),
    CHECK (freshness_status <> 'CURRENT' OR evidence_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_browser_live_facts_display
    ON browser_live_facts (tenant_id, session_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_browser_live_facts_context
    ON browser_live_facts (tenant_id, entity_key, variant_key, account_context_hash, fact_type);

CREATE TABLE IF NOT EXISTS browser_live_fact_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    fact_id UUID NOT NULL REFERENCES browser_live_facts(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('CURRENT','STALE','UNAVAILABLE','CONFLICT')),
    value_hash TEXT NOT NULL,
    evidence_id TEXT,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_browser_live_fact_events_fact
    ON browser_live_fact_events (tenant_id, fact_id, created_at DESC);
