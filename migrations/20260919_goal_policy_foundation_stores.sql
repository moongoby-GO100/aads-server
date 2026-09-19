-- W-12b/W-12c policy foundation stores and tenant fences.
-- Additive and repeatable. Existing M12/M14 migrations remain immutable.
BEGIN;

CREATE OR REPLACE FUNCTION aads_current_tenant_id() RETURNS UUID
LANGUAGE plpgsql STABLE AS $$
DECLARE value TEXT;
BEGIN
    value := current_setting('app.current_tenant_id', true);
    IF value IS NULL OR btrim(value) = '' THEN RETURN NULL; END IF;
    BEGIN RETURN value::UUID; EXCEPTION WHEN invalid_text_representation THEN RETURN NULL; END;
END $$;

-- Existing stores: retain their public shape while completing their canonical
-- responsibilities. Defaults only affect new records; no legacy rows are rewritten.
ALTER TABLE work_item_dependencies
    ADD COLUMN IF NOT EXISTS dependency_class TEXT DEFAULT 'execution',
    ADD COLUMN IF NOT EXISTS condition_snapshot_hash TEXT,
    ADD COLUMN IF NOT EXISTS version BIGINT DEFAULT 1;

ALTER TABLE work_item_evidence
    ADD COLUMN IF NOT EXISTS work_item_version BIGINT,
    ADD COLUMN IF NOT EXISTS criterion_key TEXT,
    ADD COLUMN IF NOT EXISTS artifact_hash TEXT,
    ADD COLUMN IF NOT EXISTS verification_state TEXT DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS verified_by UUID,
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS retention_class TEXT DEFAULT 'standard',
    ADD COLUMN IF NOT EXISTS erased JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS masked JSONB DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS masking_policy_version BIGINT;

ALTER TABLE goal_workflow_outbox
    ADD COLUMN IF NOT EXISTS decision_id UUID,
    ADD COLUMN IF NOT EXISTS payload_hash TEXT,
    ADD COLUMN IF NOT EXISTS sequence_no BIGINT,
    ADD COLUMN IF NOT EXISTS claim_token UUID,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS publish_state TEXT DEFAULT 'pending';

CREATE UNIQUE INDEX IF NOT EXISTS ux_goal_policy_versions_tenant_id
    ON goal_approval_policy_versions(id, tenant_id);

CREATE TABLE IF NOT EXISTS goal_kill_switches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    goal_id UUID,
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('tenant','project','goal')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    epoch BIGINT NOT NULL CHECK (epoch > 0),
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    changed_by UUID NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    previous_id UUID,
    CONSTRAINT fk_goal_kill_switch_goal FOREIGN KEY (goal_id, tenant_id, project)
        REFERENCES goals(id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_kill_switch_previous FOREIGN KEY (previous_id, tenant_id, project)
        REFERENCES goal_kill_switches(id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_kill_switch_actor FOREIGN KEY (changed_by, tenant_id)
        REFERENCES chat_sessions(id, tenant_id) ON DELETE RESTRICT,
    CONSTRAINT ck_goal_kill_switch_scope CHECK
        ((scope_kind = 'goal' AND goal_id IS NOT NULL) OR (scope_kind <> 'goal' AND goal_id IS NULL)),
    UNIQUE NULLS NOT DISTINCT (tenant_id, project, goal_id, epoch),
    UNIQUE (id, tenant_id, project)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_goal_kill_switch_current_scope
    ON goal_kill_switches
       (tenant_id, project, scope_kind, COALESCE(goal_id, '00000000-0000-0000-0000-000000000000'::uuid))
    WHERE active;

CREATE TABLE IF NOT EXISTS goal_policy_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    workspace_kind TEXT NOT NULL CHECK (workspace_kind IN ('project','ceo')),
    principal_session_id UUID NOT NULL,
    assignment_id UUID,
    target_type TEXT NOT NULL CHECK (target_type IN ('goal','milestone','epic','story','task','change_set')),
    target_id UUID NOT NULL,
    action TEXT NOT NULL,
    base_version BIGINT NOT NULL CHECK (base_version > 0),
    environment TEXT NOT NULL CHECK (environment IN ('dev','staging','production')),
    boundary_decision TEXT NOT NULL CHECK (boundary_decision IN ('ALLOW','DENY')),
    approval_route TEXT NOT NULL CHECK (approval_route IN
        ('NONE','NOTIFY','PROJECT_APPROVAL','CEO_APPROVAL','INDEPENDENT_REVIEW',
         'PROJECT_AND_INDEPENDENT','CEO_AND_INDEPENDENT')),
    automation_eligibility TEXT NOT NULL CHECK (automation_eligibility IN
        ('BASELINE_AUTO','GRANT_REQUIRED','MANDATORY_HUMAN','NOT_EXECUTABLE')),
    risk_tier TEXT NOT NULL CHECK (risk_tier IN ('A0','A1','A2','A3')),
    result TEXT NOT NULL CHECK (result IN ('AUTO','APPROVAL_REQUIRED','DENY','NOT_EXECUTABLE')),
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    policy_version UUID NOT NULL,
    matched_grant_id UUID,
    grant_version INTEGER,
    decision_input_hash TEXT NOT NULL CHECK (decision_input_hash ~ '^sha256:[0-9a-f]{64}$'),
    patch_hash TEXT CHECK (patch_hash IS NULL OR patch_hash ~ '^sha256:[0-9a-f]{64}$'),
    precondition_snapshot_hash TEXT NOT NULL CHECK (precondition_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'),
    canonicalization_version TEXT NOT NULL,
    hash_algorithm TEXT NOT NULL CHECK (hash_algorithm = 'SHA-256'),
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    original_engine_result TEXT,
    effective_application_result TEXT NOT NULL,
    erased JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(erased) = 'array'),
    masked JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(masked) = 'array'),
    masking_policy_version BIGINT NOT NULL CHECK (masking_policy_version > 0),
    kill_switch_epoch BIGINT NOT NULL CHECK (kill_switch_epoch >= 0),
    deny_policy_epoch BIGINT NOT NULL CHECK (deny_policy_epoch >= 0),
    assignment_epoch BIGINT NOT NULL CHECK (assignment_epoch >= 0),
    grant_revocation_epoch BIGINT NOT NULL CHECK (grant_revocation_epoch >= 0),
    target_version BIGINT NOT NULL CHECK (target_version > 0),
    trace_id TEXT, span_id TEXT, correlation_id TEXT,
    signature_algorithm TEXT NOT NULL,
    signature TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    CONSTRAINT fk_goal_policy_decision_session FOREIGN KEY (principal_session_id, tenant_id)
        REFERENCES chat_sessions(id, tenant_id) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_policy_decision_assignment FOREIGN KEY (assignment_id, tenant_id, project)
        REFERENCES project_role_assignments(id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_policy_decision_policy FOREIGN KEY (policy_version, tenant_id)
        REFERENCES goal_approval_policy_versions(id, tenant_id) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_policy_decision_grant FOREIGN KEY (tenant_id, matched_grant_id, grant_version)
        REFERENCES goal_auto_approval_grants(tenant_id, id, grant_version) ON DELETE RESTRICT,
    CONSTRAINT ck_goal_policy_decision_grant_shape CHECK
        ((matched_grant_id IS NULL AND grant_version IS NULL) OR
         (matched_grant_id IS NOT NULL AND grant_version IS NOT NULL)),
    UNIQUE (id, tenant_id, project),
    UNIQUE (tenant_id, decision_input_hash)
);

CREATE TABLE IF NOT EXISTS goal_execution_leases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    execution_key TEXT NOT NULL,
    decision_id UUID NOT NULL,
    owner_instance TEXT NOT NULL CHECK (btrim(owner_instance) <> ''),
    owner_epoch BIGINT NOT NULL CHECK (owner_epoch > 0),
    lease_state TEXT NOT NULL DEFAULT 'claimed' CHECK (lease_state IN
        ('claimed','executing','cancellation_requested','finish_current_requested','cancelled',
         'executed','compensating','compensated','compensation_failed','reconciliation_required','expired')),
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    reconciliation_state TEXT NOT NULL DEFAULT 'none' CHECK (reconciliation_state IN
        ('none','pending','confirmed','refund_prohibited','retry_prohibited','manual_required','resolved')),
    result_hash TEXT CHECK (result_hash IS NULL OR result_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT fk_goal_execution_lease_decision FOREIGN KEY (decision_id, tenant_id, project)
        REFERENCES goal_policy_decisions(id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT ck_goal_execution_lease_window CHECK (expires_at > acquired_at),
    UNIQUE (tenant_id, execution_key),
    UNIQUE (tenant_id, project, execution_key, owner_epoch)
);

CREATE TABLE IF NOT EXISTS goal_auto_approval_use_reservations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    execution_key TEXT NOT NULL,
    decision_id UUID NOT NULL,
    grant_id UUID NOT NULL,
    grant_version INTEGER NOT NULL,
    reservation_state TEXT NOT NULL CHECK (reservation_state IN
        ('reserved','executing','completed','released','failed','budget_overrun','reconciliation_required')),
    estimated_budget JSONB NOT NULL DEFAULT '{}'::jsonb,
    actual_budget JSONB NOT NULL DEFAULT '{}'::jsonb,
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    reconciled_at TIMESTAMPTZ,
    reconciliation_state TEXT NOT NULL DEFAULT 'pending' CHECK (reconciliation_state IN
        ('pending','settled','completed_with_budget_overrun','reconciliation_required','manual_required')),
    CONSTRAINT fk_goal_use_reservation_decision FOREIGN KEY (decision_id, tenant_id, project)
        REFERENCES goal_policy_decisions(id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_use_reservation_grant FOREIGN KEY (tenant_id, grant_id, grant_version)
        REFERENCES goal_auto_approval_grants(tenant_id, id, grant_version) ON DELETE RESTRICT,
    UNIQUE (tenant_id, execution_key),
    UNIQUE (tenant_id, decision_id),
    UNIQUE (id, tenant_id, project)
);

CREATE TABLE IF NOT EXISTS goal_auto_approval_use_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    reservation_id UUID NOT NULL,
    execution_key TEXT NOT NULL,
    decision_id UUID NOT NULL,
    grant_id UUID NOT NULL,
    grant_version INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN
        ('reserved','executing','completed','released','failed','budget_overrun','reconciliation_required','reconciled')),
    sequence_no BIGINT NOT NULL CHECK (sequence_no > 0),
    budget_delta JSONB NOT NULL DEFAULT '{}'::jsonb,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    CONSTRAINT fk_goal_use_event_reservation FOREIGN KEY (reservation_id, tenant_id, project)
        REFERENCES goal_auto_approval_use_reservations(id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_use_event_decision FOREIGN KEY (decision_id, tenant_id, project)
        REFERENCES goal_policy_decisions(id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_use_event_grant FOREIGN KEY (tenant_id, grant_id, grant_version)
        REFERENCES goal_auto_approval_grants(tenant_id, id, grant_version) ON DELETE RESTRICT,
    UNIQUE (tenant_id, execution_key, sequence_no),
    UNIQUE (tenant_id, reservation_id, event_type, sequence_no)
);

CREATE TABLE IF NOT EXISTS work_item_review_requirements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    goal_id UUID NOT NULL,
    work_item_id UUID NOT NULL,
    work_item_version BIGINT NOT NULL CHECK (work_item_version > 0),
    required_role_key TEXT NOT NULL,
    minimum_approvals INTEGER NOT NULL CHECK (minimum_approvals > 0),
    review_order INTEGER NOT NULL CHECK (review_order > 0),
    sla_seconds INTEGER NOT NULL CHECK (sla_seconds > 0),
    escalation_role_key TEXT NOT NULL DEFAULT 'ceo',
    independent_review BOOLEAN NOT NULL DEFAULT TRUE,
    state TEXT NOT NULL DEFAULT 'pending_assignment' CHECK (state IN
        ('pending_assignment','assigned','in_review','satisfied','changes_requested','overridden','expired')),
    due_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fk_work_item_review_requirement_scope
        FOREIGN KEY (work_item_id, tenant_id, project, goal_id)
        REFERENCES work_items(id, tenant_id, project, goal_id) ON DELETE RESTRICT,
    UNIQUE (id, tenant_id, project, goal_id, work_item_id, work_item_version),
    UNIQUE (tenant_id, work_item_id, work_item_version, required_role_key, review_order)
);

CREATE TABLE IF NOT EXISTS work_item_review_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    goal_id UUID NOT NULL,
    work_item_id UUID NOT NULL,
    work_item_version BIGINT NOT NULL CHECK (work_item_version > 0),
    requirement_id UUID NOT NULL,
    reviewer_session_id UUID NOT NULL,
    reviewer_role_key TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('accepted','rejected','changes_requested','override')),
    evidence_snapshot_hash TEXT NOT NULL CHECK (evidence_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'),
    contributor_session_ids UUID[] NOT NULL DEFAULT '{}',
    creator_session_id UUID,
    change_requester_session_id UUID,
    assigned_session_id UUID,
    execution_assignee_session_id UUID,
    evidence_submitter_session_ids UUID[] NOT NULL DEFAULT '{}',
    execution_actor_session_id UUID,
    override_reason TEXT,
    original_required_role TEXT,
    overridden_requirement_id UUID,
    risk_acceptance TEXT,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    CONSTRAINT fk_work_item_review_decision_requirement
        FOREIGN KEY (requirement_id, tenant_id, project, goal_id, work_item_id, work_item_version)
        REFERENCES work_item_review_requirements
            (id, tenant_id, project, goal_id, work_item_id, work_item_version) ON DELETE RESTRICT,
    CONSTRAINT fk_work_item_review_decision_reviewer FOREIGN KEY (reviewer_session_id, tenant_id)
        REFERENCES chat_sessions(id, tenant_id) ON DELETE RESTRICT,
    CONSTRAINT fk_work_item_review_decision_override
        FOREIGN KEY (overridden_requirement_id, tenant_id, project, goal_id, work_item_id, work_item_version)
        REFERENCES work_item_review_requirements
            (id, tenant_id, project, goal_id, work_item_id, work_item_version) ON DELETE RESTRICT,
    CONSTRAINT ck_work_item_review_independence CHECK
        (reviewer_session_id IS DISTINCT FROM creator_session_id AND
         reviewer_session_id IS DISTINCT FROM change_requester_session_id AND
         reviewer_session_id IS DISTINCT FROM assigned_session_id AND
         reviewer_session_id IS DISTINCT FROM execution_assignee_session_id AND
         reviewer_session_id IS DISTINCT FROM execution_actor_session_id AND
         NOT reviewer_session_id = ANY(evidence_submitter_session_ids) AND
         NOT reviewer_session_id = ANY(contributor_session_ids)),
    CONSTRAINT ck_work_item_review_override CHECK
        ((verdict <> 'override' AND override_reason IS NULL AND original_required_role IS NULL
          AND overridden_requirement_id IS NULL AND risk_acceptance IS NULL) OR
         (verdict = 'override' AND override_reason IS NOT NULL AND original_required_role IS NOT NULL
          AND overridden_requirement_id IS NOT NULL AND risk_acceptance IS NOT NULL)),
    UNIQUE (tenant_id, requirement_id, reviewer_session_id)
);

CREATE INDEX IF NOT EXISTS ix_goal_policy_decisions_target
    ON goal_policy_decisions(tenant_id, project, target_type, target_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS ix_goal_execution_leases_expiry
    ON goal_execution_leases(tenant_id, lease_state, expires_at);
CREATE INDEX IF NOT EXISTS ix_goal_use_reservations_grant
    ON goal_auto_approval_use_reservations(tenant_id, grant_id, reservation_state);
CREATE INDEX IF NOT EXISTS ix_review_requirements_due
    ON work_item_review_requirements(tenant_id, state, due_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_work_item_change_sets_scope
    ON work_item_change_sets(id, tenant_id, project);
CREATE UNIQUE INDEX IF NOT EXISTS ux_goal_workflow_outbox_sequence
    ON goal_workflow_outbox(tenant_id, execution_key, sequence_no)
    WHERE sequence_no IS NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_goal_outbox_change_set_scope'
                   AND conrelid='goal_workflow_outbox'::regclass) THEN
        ALTER TABLE goal_workflow_outbox ADD CONSTRAINT fk_goal_outbox_change_set_scope
            FOREIGN KEY (change_set_id, tenant_id, project)
            REFERENCES work_item_change_sets(id, tenant_id, project) ON DELETE RESTRICT NOT VALID;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_goal_outbox_decision_scope'
                   AND conrelid='goal_workflow_outbox'::regclass) THEN
        ALTER TABLE goal_workflow_outbox ADD CONSTRAINT fk_goal_outbox_decision_scope
            FOREIGN KEY (decision_id, tenant_id, project)
            REFERENCES goal_policy_decisions(id, tenant_id, project) ON DELETE RESTRICT NOT VALID;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION aads_require_new_foundation_fields() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'work_item_dependencies' THEN
        IF NEW.dependency_class NOT IN ('execution','information') OR NEW.version IS NULL OR NEW.version <= 0 THEN
            RAISE EXCEPTION 'invalid dependency foundation fields' USING ERRCODE='23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'work_item_evidence' THEN
        IF NEW.work_item_version IS NULL OR NEW.work_item_version <= 0 OR NEW.criterion_key IS NULL
           OR COALESCE(NEW.artifact_hash, NEW.content_hash) !~ '^sha256:[0-9a-f]{64}$'
           OR NEW.verification_state NOT IN ('pending','verified','rejected','expired')
           OR NEW.retention_class NOT IN ('standard','audit','legal_hold','ephemeral') THEN
            RAISE EXCEPTION 'invalid evidence foundation fields' USING ERRCODE='23514';
        END IF;
    ELSIF TG_TABLE_NAME = 'goal_workflow_outbox' THEN
        -- Preserve the accepted M14 writer until W-14F starts supplying these
        -- values explicitly. jsonb::text is stable for the legacy payload.
        NEW.payload_hash := COALESCE(NEW.payload_hash,
            'sha256:' || encode(digest(convert_to(NEW.payload::text, 'UTF8'), 'sha256'), 'hex'));
        NEW.sequence_no := COALESCE(NEW.sequence_no, 1);
        NEW.publish_state := COALESCE(NEW.publish_state, 'pending');
        IF NEW.payload_hash !~ '^sha256:[0-9a-f]{64}$' OR NEW.sequence_no <= 0
           OR NEW.publish_state NOT IN ('pending','claimed','published','failed','reconciliation_required') THEN
            RAISE EXCEPTION 'invalid outbox foundation fields' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER trg_dependency_foundation_fields BEFORE INSERT OR UPDATE ON work_item_dependencies
FOR EACH ROW EXECUTE FUNCTION aads_require_new_foundation_fields();
CREATE OR REPLACE TRIGGER trg_evidence_foundation_fields BEFORE INSERT OR UPDATE ON work_item_evidence
FOR EACH ROW EXECUTE FUNCTION aads_require_new_foundation_fields();
CREATE OR REPLACE TRIGGER trg_outbox_foundation_fields BEFORE INSERT OR UPDATE ON goal_workflow_outbox
FOR EACH ROW EXECUTE FUNCTION aads_require_new_foundation_fields();

CREATE OR REPLACE FUNCTION aads_guard_kill_switch_epoch() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE previous_epoch BIGINT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        NEW.tenant_id::text || ':' || NEW.project || ':' || NEW.scope_kind || ':' || COALESCE(NEW.goal_id::text,''), 12));
    SELECT max(epoch) INTO previous_epoch FROM goal_kill_switches
     WHERE tenant_id=NEW.tenant_id AND project=NEW.project AND scope_kind=NEW.scope_kind
       AND goal_id IS NOT DISTINCT FROM NEW.goal_id;
    IF previous_epoch IS NOT NULL AND NEW.epoch <= previous_epoch THEN
        RAISE EXCEPTION 'kill switch epoch must increase' USING ERRCODE='23514';
    END IF;
    IF NOT NEW.active AND NOT EXISTS (
        SELECT 1 FROM goal_kill_switches WHERE tenant_id=NEW.tenant_id AND project=NEW.project
          AND scope_kind=NEW.scope_kind AND goal_id IS NOT DISTINCT FROM NEW.goal_id AND active
    ) THEN RAISE EXCEPTION 'cannot deactivate absent kill switch' USING ERRCODE='23514'; END IF;
    -- Deactivation is a new epoch record. Grants are deliberately not restored.
    IF NOT NEW.active THEN
        UPDATE goal_kill_switches SET active=FALSE
         WHERE tenant_id=NEW.tenant_id AND project=NEW.project AND scope_kind=NEW.scope_kind
           AND goal_id IS NOT DISTINCT FROM NEW.goal_id AND active;
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER trg_goal_kill_switch_epoch BEFORE INSERT ON goal_kill_switches
FOR EACH ROW EXECUTE FUNCTION aads_guard_kill_switch_epoch();

CREATE OR REPLACE FUNCTION aads_guard_execution_lease_fence() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id <> OLD.tenant_id OR NEW.project <> OLD.project
       OR NEW.execution_key <> OLD.execution_key OR NEW.decision_id <> OLD.decision_id THEN
        RAISE EXCEPTION 'execution lease identity is immutable' USING ERRCODE='55000';
    END IF;
    IF (NEW.owner_instance, NEW.owner_epoch) IS DISTINCT FROM (OLD.owner_instance, OLD.owner_epoch)
       AND NEW.owner_epoch <= OLD.owner_epoch THEN
        RAISE EXCEPTION 'execution lease owner epoch must increase' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER trg_goal_execution_lease_fence BEFORE UPDATE ON goal_execution_leases
FOR EACH ROW EXECUTE FUNCTION aads_guard_execution_lease_fence();

CREATE OR REPLACE TRIGGER trg_goal_policy_decisions_append_only BEFORE UPDATE OR DELETE ON goal_policy_decisions
FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();
CREATE OR REPLACE TRIGGER trg_goal_use_events_append_only BEFORE UPDATE OR DELETE ON goal_auto_approval_use_events
FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();
CREATE OR REPLACE TRIGGER trg_review_decisions_append_only BEFORE UPDATE OR DELETE ON work_item_review_decisions
FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();

-- Database-enforced tenant boundary. FORCE also applies policies to table owners;
-- deployment must use a NOSUPERUSER NOBYPASSRLS runtime role (ADR-021).
DO $$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'work_item_dependencies','work_item_evidence','goal_kill_switches','goal_policy_decisions',
        'goal_workflow_outbox','goal_execution_leases','goal_auto_approval_use_reservations',
        'goal_auto_approval_use_events','work_item_review_requirements','work_item_review_decisions'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname=current_schema()
                       AND tablename=table_name AND policyname='tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = aads_current_tenant_id()) WITH CHECK (tenant_id = aads_current_tenant_id())', table_name);
        END IF;
    END LOOP;
END $$;

-- Explicit read-only migration path for overlapping M14 legacy tables.
-- Canonical writes go only to the new stores; security invoker preserves RLS.
CREATE OR REPLACE VIEW goal_approval_kill_switches_compat
WITH (security_invoker=true) AS
SELECT id, tenant_id, project, goal_id, active, reason,
       changed_by AS activated_by, changed_at AS activated_at,
       CASE WHEN active THEN NULL ELSE changed_by END AS deactivated_by,
       CASE WHEN active THEN NULL ELSE changed_at END AS deactivated_at,
       epoch
  FROM goal_kill_switches;

CREATE OR REPLACE VIEW goal_approval_decision_logs_compat
WITH (security_invoker=true) AS
SELECT id, tenant_id, result AS decision, policy_version,
       matched_grant_id, reason_codes, diagnostics AS input_context,
       FALSE AS simulated, NULL::TEXT AS shadow_decision, decided_at AS created_at,
       decision_input_hash, precondition_snapshot_hash
  FROM goal_policy_decisions;

COMMIT;
