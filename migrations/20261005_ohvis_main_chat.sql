-- OHVIS main chat: durable report inbox, reviews, grants, effect outbox, audit and in-app notices.
-- Additive only: new tables, no change to any existing table or constraint. Nothing here sends anything outside OHVIS.
-- Plain uuids (no FK to chat/goal tables) so chat cleanup can never block the inbox or the audit trail.
-- Automatic effects stay off until OHVIS_MAIN_CHAT_AUTO_EFFECT is set by an approved rollout; this schema alone enables nothing.
BEGIN;

CREATE TABLE IF NOT EXISTS ohvis_main_chat_routes (
    id bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    goal_id uuid,
    main_session_id uuid NOT NULL,
    accepts_goal_events boolean NOT NULL DEFAULT false,
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    enabled boolean NOT NULL DEFAULT true,
    created_by text NOT NULL,
    updated_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (goal_id IS NULL OR accepts_goal_events = false)
);
CREATE UNIQUE INDEX IF NOT EXISTS ohvis_main_chat_routes_scope_uq
    ON ohvis_main_chat_routes (tenant_id, project_key, COALESCE(goal_id, '00000000-0000-0000-0000-000000000000'::uuid));

CREATE TABLE IF NOT EXISTS ohvis_main_chat_control (
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    paused boolean NOT NULL DEFAULT false,
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    reason text NOT NULL DEFAULT '',
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_key)
);

CREATE TABLE IF NOT EXISTS ohvis_report_inbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    source_kind text NOT NULL CHECK (source_kind IN ('runner', 'agent')),
    source_event_id text NOT NULL,
    source_revision integer NOT NULL CHECK (source_revision >= 1),
    schema_version integer NOT NULL,
    event_type text NOT NULL,
    project_key text NOT NULL,
    goal_id uuid,
    root_task_id text NOT NULL,
    correlation_id text NOT NULL,
    causation_id text,
    runner_job_id text,
    source_session_id uuid,
    main_session_id uuid,
    route_revision integer,
    generation_id text,
    commit_sha text,
    diff_sha256 text,
    payload jsonb NOT NULL,
    payload_sha256 text NOT NULL,
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL CHECK (state IN ('received', 'routed', 'pending_review', 'reviewing', 'reviewed',
        'waiting_session', 'waiting_route', 'waiting_evidence', 'quarantined', 'dead_letter', 'obsolete')),
    -- separate counters: only model_call_attempts is a model retry; claims and delivery never consume it.
    delivery_attempts integer NOT NULL DEFAULT 0,
    claim_count integer NOT NULL DEFAULT 0,
    model_call_attempts integer NOT NULL DEFAULT 0,
    owner_instance text,
    owner_epoch bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_kind, source_event_id)
);
CREATE INDEX IF NOT EXISTS ohvis_report_inbox_queue_idx
    ON ohvis_report_inbox (state, received_at) WHERE state IN ('pending_review', 'waiting_session', 'reviewing');
CREATE INDEX IF NOT EXISTS ohvis_report_inbox_root_idx
    ON ohvis_report_inbox (tenant_id, project_key, root_task_id, source_kind, source_revision DESC);

CREATE TABLE IF NOT EXISTS ohvis_report_reviews (
    id bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    inbox_id uuid NOT NULL REFERENCES ohvis_report_inbox(id),
    review_revision integer NOT NULL CHECK (review_revision >= 1),
    decision text NOT NULL CHECK (decision IN ('verified', 'needs_rework', 'needs_decision', 'unverifiable', 'obsolete')),
    code_verified boolean NOT NULL DEFAULT false,
    reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_sha256 text NOT NULL,
    owner_epoch bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (inbox_id, review_revision)
);

CREATE TABLE IF NOT EXISTS ohvis_action_grants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    goal_id uuid,
    root_task_id text NOT NULL,
    grant_type text NOT NULL CHECK (grant_type IN ('design_approval', 'code_review', 'execute_followup', 'push', 'deploy')),
    scope jsonb NOT NULL,
    target_hash text NOT NULL,
    generation_id text,
    commit_sha text,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_reason text,
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    issuer text NOT NULL,
    approval_ref text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ohvis_action_grants_scope_idx
    ON ohvis_action_grants (tenant_id, project_key, root_task_id, grant_type);

CREATE TABLE IF NOT EXISTS ohvis_action_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    inbox_id uuid NOT NULL REFERENCES ohvis_report_inbox(id),
    review_id bigint NOT NULL REFERENCES ohvis_report_reviews(id),
    project_key text NOT NULL,
    root_task_id text NOT NULL,
    effect_key text NOT NULL,
    action text NOT NULL CHECK (action IN ('submit_followup_job', 'push', 'deploy')),
    payload jsonb NOT NULL,
    payload_sha256 text NOT NULL,
    grant_id uuid NOT NULL REFERENCES ohvis_action_grants(id),
    grant_revision integer NOT NULL,
    owner_epoch bigint NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'authorized', 'dispatching', 'confirmed', 'verified', 'unknown',
        'blocked', 'failed')),
    remote_ref text,
    dispatch_attempts integer NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, effect_key)
);

CREATE TABLE IF NOT EXISTS ohvis_main_chat_audit (
    id bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    ref jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason text NOT NULL DEFAULT '',
    owner_epoch bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ohvis_main_chat_audit_idx ON ohvis_main_chat_audit (tenant_id, created_at DESC);

CREATE OR REPLACE FUNCTION ohvis_main_chat_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ohvis_main_chat_audit is append-only';
END;
$$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'ohvis_main_chat_audit_immutable') THEN
        CREATE TRIGGER ohvis_main_chat_audit_immutable BEFORE UPDATE OR DELETE ON ohvis_main_chat_audit
            FOR EACH ROW EXECUTE FUNCTION ohvis_main_chat_audit_append_only();
    END IF;
END;
$$;

-- In-app notices only. Deliberately separate from ohvis_notifications, whose kind CHECK belongs to mockup reviews.
CREATE TABLE IF NOT EXISTS ohvis_main_chat_notices (
    id bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    project_key text NOT NULL,
    recipient_user_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('report_arrived', 'decision_needed', 'verified', 'blocked', 'effect_result')),
    inbox_id uuid,
    title text NOT NULL,
    body text NOT NULL DEFAULT '',
    link text NOT NULL,
    dedupe_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at timestamptz,
    UNIQUE (tenant_id, recipient_user_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS ohvis_main_chat_notices_inbox_idx
    ON ohvis_main_chat_notices (tenant_id, recipient_user_id, created_at DESC);

COMMIT;
