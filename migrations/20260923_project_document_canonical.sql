-- M1 canonical project documents. Legacy document and artifact tables are untouched.
BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS project_document_grants (
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    project_key text NOT NULL,
    user_id text NOT NULL,
    access text NOT NULL CHECK (access IN ('read', 'write', 'approve')),
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_key, user_id, access)
);

CREATE TABLE IF NOT EXISTS project_document_heads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    project_key text NOT NULL CHECK (project_key ~ '^[A-Z0-9][A-Z0-9_-]{0,63}$'),
    document_key text NOT NULL CHECK (document_key ~ '^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$'),
    kind text NOT NULL CHECK (kind IN ('plan','prd','spec','design','architecture','contract','tasks','report','reference')),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    latest_revision_id uuid,
    approved_revision_id uuid,
    generation bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, project_key, document_key),
    UNIQUE (id, tenant_id, project_key)
);

CREATE TABLE IF NOT EXISTS project_document_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    head_id uuid NOT NULL REFERENCES project_document_heads(id),
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    version text NOT NULL CHECK (version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    content text NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 262144),
    content_hash char(64) NOT NULL CHECK (
        content_hash ~ '^[0-9a-f]{64}$'
        AND content_hash = encode(digest(convert_to(content, 'UTF8'), 'sha256'), 'hex')
    ),
    source_path text CHECK (
        source_path IS NULL OR
        (source_path ~ '^(docs|reports)/[^[:cntrl:]]+$'
         AND source_path !~ '(^|/)\.\.(/|$)'
         AND source_path !~ '(^|/)\.([^/]|$)'
         AND source_path !~ '://')
    ),
    source_kind text NOT NULL CHECK (source_kind IN ('api','repository')),
    CHECK ((source_kind = 'repository') = (source_path IS NOT NULL)),
    source_task_id text,
    source_session_id uuid,
    goal_id uuid,
    change_summary text,
    author_id text NOT NULL,
    idempotency_key text,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (head_id, tenant_id, project_key)
        REFERENCES project_document_heads(id, tenant_id, project_key),
    FOREIGN KEY (goal_id, tenant_id, project_key)
        REFERENCES goals(id, tenant_id, project),
    UNIQUE (head_id, revision),
    UNIQUE (head_id, version),
    UNIQUE (head_id, content_hash),
    UNIQUE (head_id, idempotency_key),
    UNIQUE (head_id, id),
    UNIQUE (id, tenant_id, project_key)
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='project_document_latest_fk') THEN
        ALTER TABLE project_document_heads ADD CONSTRAINT project_document_latest_fk
        FOREIGN KEY (id, latest_revision_id) REFERENCES project_document_revisions(head_id, id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='project_document_approved_fk') THEN
        ALTER TABLE project_document_heads ADD CONSTRAINT project_document_approved_fk
        FOREIGN KEY (id, approved_revision_id) REFERENCES project_document_revisions(head_id, id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS project_document_events (
    id bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    head_id uuid NOT NULL REFERENCES project_document_heads(id),
    revision_id uuid,
    action text NOT NULL CHECK (action IN ('created','revised','review','approved','archived','goal_linked','legacy_linked')),
    actor_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (head_id, revision_id) REFERENCES project_document_revisions(head_id, id)
);

CREATE TABLE IF NOT EXISTS project_document_goal_links (
    head_id uuid NOT NULL REFERENCES project_document_heads(id),
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    goal_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (head_id, goal_id),
    FOREIGN KEY (head_id, tenant_id, project_key)
        REFERENCES project_document_heads(id, tenant_id, project_key),
    FOREIGN KEY (goal_id, tenant_id, project_key)
        REFERENCES goals(id, tenant_id, project)
);

CREATE TABLE IF NOT EXISTS project_document_legacy_links (
    revision_id uuid NOT NULL REFERENCES project_document_revisions(id),
    goal_document_id bigint NOT NULL UNIQUE REFERENCES goal_documents(id) ON DELETE RESTRICT,
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    goal_id uuid NOT NULL,
    validated_path text NOT NULL,
    validated_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (revision_id, goal_document_id),
    FOREIGN KEY (revision_id, tenant_id, project_key)
        REFERENCES project_document_revisions(id, tenant_id, project_key),
    FOREIGN KEY (goal_id, tenant_id, project_key)
        REFERENCES goals(id, tenant_id, project)
);

CREATE INDEX IF NOT EXISTS project_document_list_idx
    ON project_document_heads(tenant_id, project_key, updated_at DESC);
CREATE INDEX IF NOT EXISTS project_document_revision_idx
    ON project_document_revisions(head_id, revision DESC);
CREATE INDEX IF NOT EXISTS project_document_goal_idx
    ON project_document_goal_links(tenant_id, project_key, goal_id);

CREATE OR REPLACE FUNCTION reject_project_document_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'project document history is append-only';
END $$;
DROP TRIGGER IF EXISTS project_document_revisions_immutable ON project_document_revisions;
CREATE TRIGGER project_document_revisions_immutable BEFORE UPDATE OR DELETE
    ON project_document_revisions FOR EACH ROW EXECUTE FUNCTION reject_project_document_history_mutation();
DROP TRIGGER IF EXISTS project_document_events_immutable ON project_document_events;
CREATE TRIGGER project_document_events_immutable BEFORE UPDATE OR DELETE
    ON project_document_events FOR EACH ROW EXECUTE FUNCTION reject_project_document_history_mutation();
DROP TRIGGER IF EXISTS project_document_legacy_links_immutable ON project_document_legacy_links;
CREATE TRIGGER project_document_legacy_links_immutable BEFORE UPDATE OR DELETE
    ON project_document_legacy_links FOR EACH ROW EXECUTE FUNCTION reject_project_document_history_mutation();

-- Handover is staged with the schema and runs only when the approved migration runs.
WITH changed AS (
    INSERT INTO project_handover_entries
        (tenant_id,project_key,entry_key,entry_type,title,summary,body,status,priority,
         source_kind,source_task_id,source_path,metadata,created_by)
    VALUES
        (public.aads_internal_tenant_id(),'AADS','aads-project-documents-canonical-20260923',
         'task','Project document canonical ledger M1',
         'Project-scoped immutable revisions, independent latest and approved pointers, bounded read-only brief.',
         'M1 schema applied: additive project_document tables and authenticated API contract. Legacy goal_documents, project_artifacts, and chat_artifacts remain unchanged. Backfill is inventory only. M2: connect approved_brief after DB-map review resolves; reject missing/unapproved documents as authoritative.',
         'active','P1','migration','AADS-PROJECT-DOCUMENTS-M1-20260923',
         'migrations/20260923_project_document_canonical.sql',
         '{"m1_schema":"applied","m2":"pending_db_map_review","legacy_backfill":"inventory_only"}'::jsonb,
         'pipeline_runner')
    ON CONFLICT (tenant_id,project_key,entry_key) DO UPDATE SET
        metadata=project_handover_entries.metadata || EXCLUDED.metadata,
        revision=project_handover_entries.revision+1, updated_at=now()
    WHERE project_handover_entries.metadata IS DISTINCT FROM
          (project_handover_entries.metadata || EXCLUDED.metadata)
    RETURNING *
)
INSERT INTO project_handover_events
    (entry_id,tenant_id,project_key,event_type,revision,snapshot,change_summary,changed_by)
SELECT id,tenant_id,project_key,CASE WHEN revision=1 THEN 'created' ELSE 'updated' END,
       revision,to_jsonb(changed)-'search_vector','M1 code and schema staged','pipeline_runner'
FROM changed;

COMMIT;
