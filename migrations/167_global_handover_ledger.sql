-- 167: Global, tenant-scoped handover ledger.
-- The database is canonical; Markdown is an import/export compatibility format.
-- This migration is additive and idempotent. It never deletes legacy files or rows.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS project_handover_entries (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_key       VARCHAR(64) NOT NULL,
    entry_key         VARCHAR(200) NOT NULL,
    entry_type        VARCHAR(32) NOT NULL DEFAULT 'note'
                      CHECK (entry_type IN ('status', 'decision', 'task', 'risk', 'verification', 'note')),
    title             VARCHAR(300) NOT NULL,
    summary           TEXT,
    body              TEXT NOT NULL,
    status            VARCHAR(24) NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'resolved', 'superseded', 'archived')),
    priority          VARCHAR(8) NOT NULL DEFAULT 'P2'
                      CHECK (priority IN ('P0', 'P1', 'P2', 'P3')),
    source_kind       VARCHAR(32) NOT NULL DEFAULT 'api',
    source_session_id UUID,
    source_task_id    TEXT,
    source_path       TEXT,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    revision          INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at       TIMESTAMPTZ,
    search_vector     TSVECTOR GENERATED ALWAYS AS (
        to_tsvector(
            'simple'::regconfig,
            COALESCE(title, '') || ' ' || COALESCE(summary, '') || ' ' || COALESCE(body, '')
        )
    ) STORED,
    UNIQUE (tenant_id, project_key, entry_key)
);

CREATE TABLE IF NOT EXISTS project_handover_events (
    id             BIGSERIAL PRIMARY KEY,
    entry_id       UUID NOT NULL REFERENCES project_handover_entries(id) ON DELETE CASCADE,
    tenant_id      UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_key    VARCHAR(64) NOT NULL,
    event_type     VARCHAR(24) NOT NULL
                   CHECK (event_type IN ('created', 'updated', 'status_changed', 'imported')),
    revision       INTEGER NOT NULL CHECK (revision > 0),
    snapshot       JSONB NOT NULL,
    change_summary TEXT,
    changed_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entry_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_handover_entries_project_status
    ON project_handover_entries (tenant_id, project_key, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_handover_entries_type
    ON project_handover_entries (tenant_id, project_key, entry_type, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_handover_entries_search
    ON project_handover_entries USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_handover_entries_title_trgm
    ON project_handover_entries USING GIN (LOWER(title) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_handover_entries_path_trgm
    ON project_handover_entries USING GIN (LOWER(COALESCE(source_path, '')) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_handover_entries_metadata
    ON project_handover_entries USING GIN (metadata);
CREATE INDEX IF NOT EXISTS idx_handover_events_project
    ON project_handover_events (tenant_id, project_key, created_at DESC);

-- An event is an immutable audit fact. Application rollback leaves this data in
-- place; there is intentionally no delete endpoint or destructive migration.
CREATE OR REPLACE FUNCTION public.aads_reject_handover_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'project_handover_events is append-only';
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'trg_handover_events_append_only'
           AND tgrelid = 'public.project_handover_events'::regclass
    ) THEN
        CREATE TRIGGER trg_handover_events_append_only
        BEFORE UPDATE OR DELETE ON project_handover_events
        FOR EACH ROW EXECUTE FUNCTION public.aads_reject_handover_event_mutation();
    END IF;
END $$;

COMMENT ON TABLE project_handover_entries IS
    'Tenant-scoped current handover state shared by every AADS project and chat session';
COMMENT ON TABLE project_handover_events IS
    'Append-only revision history for project_handover_entries';
COMMENT ON COLUMN project_handover_entries.entry_key IS
    'Stable idempotency key within tenant and project';
COMMENT ON COLUMN project_handover_entries.search_vector IS
    'Full-text search; pg_trgm indexes are limited to fuzzy title/path lookup';

COMMIT;
