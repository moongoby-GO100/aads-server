-- 108: External chat gateway for NewTalk embed.

CREATE TABLE IF NOT EXISTS external_chat_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,
    service TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    display_name TEXT,
    aads_session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider, service, external_user_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_external_chat_sessions_aads_session
    ON external_chat_sessions(aads_session_id);

CREATE TABLE IF NOT EXISTS external_chat_usage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_session_id UUID REFERENCES external_chat_sessions(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    service TEXT NOT NULL,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    soft_bypass BOOLEAN NOT NULL DEFAULT FALSE,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_external_chat_usage_events_provider_created
    ON external_chat_usage_events(provider, service, created_at DESC);
