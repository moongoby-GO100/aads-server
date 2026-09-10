-- OHVIS directive copilot: reviewable draft, immutable revisions, usage events.

CREATE TABLE IF NOT EXISTS directive_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    artifact_id UUID REFERENCES chat_artifacts(id) ON DELETE SET NULL,
    created_by UUID,
    project_key VARCHAR(32) NOT NULL,
    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'approved', 'rejected', 'sent', 'archived')),
    risk_level VARCHAR(12) NOT NULL DEFAULT 'medium'
        CHECK (risk_level IN ('low', 'medium', 'high')),
    confidence NUMERIC(4,3) NOT NULL DEFAULT 0.500
        CHECK (confidence >= 0 AND confidence <= 1),
    current_revision INTEGER NOT NULL DEFAULT 1 CHECK (current_revision >= 1),
    source_message_ids UUID[] NOT NULL DEFAULT '{}',
    classification JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS directive_draft_revisions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    draft_id UUID NOT NULL REFERENCES directive_drafts(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL,
    change_source VARCHAR(24) NOT NULL
        CHECK (change_source IN ('generated', 'fallback', 'user_edit', 'regenerated', 'artifact_edit')),
    editor_user_id UUID,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (draft_id, revision)
);

CREATE TABLE IF NOT EXISTS directive_draft_events (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    draft_id UUID NOT NULL REFERENCES directive_drafts(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    action VARCHAR(20) NOT NULL
        CHECK (action IN ('created', 'edited', 'inserted', 'approved', 'rejected', 'sent', 'archived')),
    actor_user_id UUID,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_directive_drafts_tenant_session
    ON directive_drafts (tenant_id, session_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_directive_draft_revisions_tenant_draft
    ON directive_draft_revisions (tenant_id, draft_id, revision DESC);
CREATE INDEX IF NOT EXISTS idx_directive_draft_events_tenant_draft
    ON directive_draft_events (tenant_id, draft_id, created_at DESC);
