-- R-DOC mockup review: immutable revisions, change requests, audit events, task bindings.
-- Additive only. Canonical project_document_* and chat_* tables are read by the API, never written.
-- Revisions and events are append-only; heads carry the mutable pointers + monotonic generation.
-- chat session/message ids are plain uuids (no FK) so chat cleanup never blocks or erases the audit trail.
BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS mockup_review_heads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    project_key text NOT NULL CHECK (project_key ~ '^[A-Z0-9][A-Z0-9_-]{0,63}$'),
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    change_type text NOT NULL CHECK (change_type IN ('new','modify','none')),
    goal_id uuid,
    session_id uuid,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN
        ('draft','review_ready','changes_requested','revising','approved','revoked')),
    latest_revision_id uuid,
    approved_revision_id uuid,
    approval_event_id bigint,
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    pointer_generation bigint NOT NULL DEFAULT 0 CHECK (pointer_generation >= 0),
    created_by text NOT NULL,
    create_idempotency_key text,
    create_request_hash char(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, project_key, create_idempotency_key),
    UNIQUE (id, tenant_id, project_key),
    FOREIGN KEY (goal_id, tenant_id, project_key) REFERENCES goals(id, tenant_id, project)
);

CREATE TABLE IF NOT EXISTS mockup_review_revisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    head_id uuid NOT NULL REFERENCES mockup_review_heads(id),
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    parent_revision_id uuid,
    manifest jsonb NOT NULL,
    manifest_hash char(64) NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
    doc_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    design_tokens_version text,
    source_sha text,
    pointer_applied boolean NOT NULL DEFAULT true,
    created_by text NOT NULL,
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (head_id, tenant_id, project_key)
        REFERENCES mockup_review_heads(id, tenant_id, project_key),
    FOREIGN KEY (head_id, parent_revision_id) REFERENCES mockup_review_revisions(head_id, id),
    UNIQUE (head_id, revision),
    UNIQUE (head_id, idempotency_key),
    UNIQUE (head_id, id)
);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mockup_review_latest_fk') THEN
        ALTER TABLE mockup_review_heads ADD CONSTRAINT mockup_review_latest_fk
        FOREIGN KEY (id, latest_revision_id) REFERENCES mockup_review_revisions(head_id, id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mockup_review_approved_fk') THEN
        ALTER TABLE mockup_review_heads ADD CONSTRAINT mockup_review_approved_fk
        FOREIGN KEY (id, approved_revision_id) REFERENCES mockup_review_revisions(head_id, id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS mockup_review_events (
    id bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    head_id uuid NOT NULL,
    revision_id uuid,
    action text NOT NULL CHECK (action IN
        ('created','revision_created','revision_archived_stale','submitted','changes_requested',
         'revising_started','approved','revoked','verify_allowed','verify_denied')),
    actor_id text NOT NULL,
    idempotency_key text,
    request_hash char(64),
    generation_after bigint NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    response jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (head_id, tenant_id, project_key)
        REFERENCES mockup_review_heads(id, tenant_id, project_key),
    FOREIGN KEY (head_id, revision_id) REFERENCES mockup_review_revisions(head_id, id),
    UNIQUE (id, head_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS mockup_review_events_idem_idx
    ON mockup_review_events(head_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS mockup_review_events_head_idx ON mockup_review_events(head_id, id DESC);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mockup_review_approval_event_fk') THEN
        ALTER TABLE mockup_review_heads ADD CONSTRAINT mockup_review_approval_event_fk
        FOREIGN KEY (approval_event_id, id) REFERENCES mockup_review_events(id, head_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS mockup_review_change_requests (
    id bigserial PRIMARY KEY,
    change_request_id uuid NOT NULL,
    head_id uuid NOT NULL,
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    source_message_id uuid NOT NULL,
    session_id uuid,
    base_revision_id uuid NOT NULL,
    screen_id text,
    comment text NOT NULL CHECK (length(btrim(comment)) > 0),
    requested_by text NOT NULL,
    idempotency_key text NOT NULL,
    queued boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_revision','resolved')),
    rebased_to_revision_id uuid,
    resolved_revision_id uuid,
    outcome text CHECK (outcome IN ('applied','not_applied')),
    outcome_reason text,
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'resolved') = (outcome IS NOT NULL AND resolved_revision_id IS NOT NULL
                                    AND outcome_reason IS NOT NULL AND resolved_at IS NOT NULL)),
    FOREIGN KEY (head_id, tenant_id, project_key)
        REFERENCES mockup_review_heads(id, tenant_id, project_key),
    FOREIGN KEY (head_id, base_revision_id) REFERENCES mockup_review_revisions(head_id, id),
    FOREIGN KEY (head_id, rebased_to_revision_id) REFERENCES mockup_review_revisions(head_id, id),
    FOREIGN KEY (head_id, resolved_revision_id) REFERENCES mockup_review_revisions(head_id, id),
    UNIQUE (head_id, change_request_id),
    UNIQUE (head_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS mockup_review_changes_open_idx
    ON mockup_review_change_requests(head_id, status, id);

CREATE TABLE IF NOT EXISTS mockup_review_task_bindings (
    id bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    head_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    approval_event_id bigint NOT NULL,
    task_id text NOT NULL CHECK (length(task_id) BETWEEN 1 AND 200),
    status text NOT NULL CHECK (status IN ('bound','running','held','review_required')),
    bound_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_verified_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (head_id, tenant_id, project_key)
        REFERENCES mockup_review_heads(id, tenant_id, project_key),
    FOREIGN KEY (head_id, revision_id) REFERENCES mockup_review_revisions(head_id, id),
    FOREIGN KEY (approval_event_id, head_id) REFERENCES mockup_review_events(id, head_id),
    UNIQUE (head_id, revision_id, task_id)
);

CREATE INDEX IF NOT EXISTS mockup_review_heads_scope_idx
    ON mockup_review_heads(tenant_id, project_key, updated_at DESC);
CREATE INDEX IF NOT EXISTS mockup_review_revisions_head_idx
    ON mockup_review_revisions(head_id, revision DESC);

CREATE OR REPLACE FUNCTION reject_mockup_review_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    RAISE EXCEPTION 'mockup review history is append-only';
END $$;

DROP TRIGGER IF EXISTS mockup_review_revisions_immutable ON mockup_review_revisions;
CREATE TRIGGER mockup_review_revisions_immutable BEFORE UPDATE OR DELETE
    ON mockup_review_revisions FOR EACH ROW EXECUTE FUNCTION reject_mockup_review_history_mutation();
DROP TRIGGER IF EXISTS mockup_review_events_immutable ON mockup_review_events;
CREATE TRIGGER mockup_review_events_immutable BEFORE UPDATE OR DELETE
    ON mockup_review_events FOR EACH ROW EXECUTE FUNCTION reject_mockup_review_history_mutation();
DROP TRIGGER IF EXISTS mockup_review_heads_no_delete ON mockup_review_heads;
CREATE TRIGGER mockup_review_heads_no_delete BEFORE DELETE
    ON mockup_review_heads FOR EACH ROW EXECUTE FUNCTION reject_mockup_review_history_mutation();
DROP TRIGGER IF EXISTS mockup_review_task_bindings_no_delete ON mockup_review_task_bindings;
CREATE TRIGGER mockup_review_task_bindings_no_delete BEFORE DELETE
    ON mockup_review_task_bindings FOR EACH ROW EXECUTE FUNCTION reject_mockup_review_history_mutation();

-- The original request text, message link and base revision are never rewritten; only the
-- queue/resolution columns move, and a resolved request is final.
CREATE OR REPLACE FUNCTION guard_mockup_review_change_request() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'mockup review change requests are append-only';
    END IF;
    IF OLD.status = 'resolved' THEN
        RAISE EXCEPTION 'resolved mockup review change request is final';
    END IF;
    IF (NEW.id, NEW.change_request_id, NEW.head_id, NEW.tenant_id, NEW.project_key, NEW.source_message_id,
        NEW.session_id, NEW.base_revision_id, NEW.screen_id, NEW.comment, NEW.requested_by,
        NEW.idempotency_key, NEW.queued, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.change_request_id, OLD.head_id, OLD.tenant_id, OLD.project_key, OLD.source_message_id,
        OLD.session_id, OLD.base_revision_id, OLD.screen_id, OLD.comment, OLD.requested_by,
        OLD.idempotency_key, OLD.queued, OLD.created_at) THEN
        RAISE EXCEPTION 'mockup review change request original fields are immutable';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS mockup_review_change_requests_guard ON mockup_review_change_requests;
CREATE TRIGGER mockup_review_change_requests_guard BEFORE UPDATE OR DELETE
    ON mockup_review_change_requests FOR EACH ROW EXECUTE FUNCTION guard_mockup_review_change_request();

COMMIT;
