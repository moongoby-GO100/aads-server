-- M12 Goal work hierarchy and governance schema.
-- Additive and repeatable. M13/M14 own the execution APIs for these tables.

BEGIN;

-- The legacy goal tables predate tenant isolation. Establish the composite
-- identities that all M12 foreign keys use instead of trusting application code.
ALTER TABLE goals ADD COLUMN IF NOT EXISTS tenant_id UUID;
UPDATE goals SET tenant_id = public.aads_internal_tenant_id() WHERE tenant_id IS NULL;
ALTER TABLE goals ALTER COLUMN tenant_id SET DEFAULT public.aads_internal_tenant_id();
ALTER TABLE goals ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE milestones ADD COLUMN IF NOT EXISTS tenant_id UUID;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS project TEXT;
UPDATE milestones m
   SET tenant_id = g.tenant_id,
       project = g.project
  FROM goals g
 WHERE m.goal_id = g.id
   AND (m.tenant_id IS NULL OR m.project IS NULL);
ALTER TABLE milestones ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE milestones ALTER COLUMN project SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_goals_id_tenant_project
    ON goals (id, tenant_id, project);
CREATE UNIQUE INDEX IF NOT EXISTS ux_milestones_id_goal_tenant_project
    ON milestones (id, goal_id, tenant_id, project);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_goals_tenant'
                   AND conrelid = 'goals'::regclass) THEN
        ALTER TABLE goals ADD CONSTRAINT fk_goals_tenant
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_milestones_goal_scope'
                   AND conrelid = 'milestones'::regclass) THEN
        ALTER TABLE milestones ADD CONSTRAINT fk_milestones_goal_scope
            FOREIGN KEY (goal_id, tenant_id, project)
            REFERENCES goals(id, tenant_id, project) ON DELETE CASCADE;
    END IF;
END $$;

-- Preserve the legacy milestone insert contract (goal_id only) while making
-- tenant/project derivation authoritative and rejecting caller mismatches.
CREATE OR REPLACE FUNCTION aads_set_milestone_scope() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE v_tenant_id UUID; v_project TEXT;
BEGIN
    SELECT tenant_id, project INTO v_tenant_id, v_project FROM goals WHERE id = NEW.goal_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'goal % not found', NEW.goal_id USING ERRCODE = '23503';
    END IF;
    IF NEW.tenant_id IS NOT NULL AND NEW.tenant_id <> v_tenant_id THEN
        RAISE EXCEPTION 'milestone tenant does not match goal' USING ERRCODE = '23514';
    END IF;
    IF NEW.project IS NOT NULL AND NEW.project <> v_project THEN
        RAISE EXCEPTION 'milestone project does not match goal' USING ERRCODE = '23514';
    END IF;
    NEW.tenant_id := v_tenant_id;
    NEW.project := v_project;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_milestone_scope ON milestones;
CREATE TRIGGER trg_milestone_scope BEFORE INSERT OR UPDATE OF goal_id,tenant_id,project
    ON milestones FOR EACH ROW EXECUTE FUNCTION aads_set_milestone_scope();

CREATE TABLE IF NOT EXISTS project_role_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL CHECK (btrim(project) <> ''),
    role_key TEXT NOT NULL CHECK (btrim(role_key) <> ''),
    session_id UUID NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    assigned_by UUID,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    ended_at TIMESTAMPTZ,
    end_reason TEXT,
    CONSTRAINT fk_project_role_assignment_session
        FOREIGN KEY (session_id, tenant_id) REFERENCES chat_sessions(id, tenant_id) ON DELETE RESTRICT,
    CONSTRAINT ck_project_role_assignment_end
        CHECK ((active AND ended_at IS NULL) OR (NOT active AND ended_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_project_role_assignments_active_role
    ON project_role_assignments (tenant_id, project, role_key) WHERE active;
CREATE UNIQUE INDEX IF NOT EXISTS ux_project_role_assignments_active_session
    ON project_role_assignments (tenant_id, project, session_id) WHERE active;
CREATE UNIQUE INDEX IF NOT EXISTS ux_project_role_assignments_id_scope
    ON project_role_assignments (id, tenant_id, project);

CREATE TABLE IF NOT EXISTS work_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    project TEXT NOT NULL CHECK (btrim(project) <> ''),
    goal_id UUID NOT NULL,
    milestone_id UUID NOT NULL,
    parent_id UUID,
    type TEXT NOT NULL CHECK (type IN ('epic', 'story', 'task')),
    title TEXT NOT NULL CHECK (btrim(title) <> ''),
    description TEXT,
    acceptance_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN
        ('draft','ready','in_progress','in_review','completed','blocked','cancelled','changes_requested')),
    priority TEXT NOT NULL DEFAULT 'P2' CHECK (priority IN ('P0','P1','P2','P3')),
    assignment_id UUID,
    progress NUMERIC(5,2) NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT fk_work_item_goal_scope FOREIGN KEY (goal_id, tenant_id, project)
        REFERENCES goals(id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT fk_work_item_milestone_scope FOREIGN KEY (milestone_id, goal_id, tenant_id, project)
        REFERENCES milestones(id, goal_id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT fk_work_item_assignment_scope FOREIGN KEY (assignment_id, tenant_id, project)
        REFERENCES project_role_assignments(id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT fk_work_item_parent_scope FOREIGN KEY (parent_id, tenant_id, project, goal_id)
        REFERENCES work_items(id, tenant_id, project, goal_id) ON DELETE RESTRICT,
    CONSTRAINT ck_work_item_completion_time CHECK
        ((status = 'completed' AND completed_at IS NOT NULL) OR status <> 'completed'),
    CONSTRAINT ck_work_item_parent_shape CHECK
        ((type = 'epic' AND parent_id IS NULL) OR (type IN ('story','task') AND parent_id IS NOT NULL)),
    CONSTRAINT ck_work_item_not_self_parent CHECK (parent_id IS NULL OR parent_id <> id),
    UNIQUE (id, tenant_id, project, goal_id),
    UNIQUE NULLS NOT DISTINCT (tenant_id, project, parent_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_work_items_tree
    ON work_items (tenant_id, project, goal_id, milestone_id, parent_id);
CREATE INDEX IF NOT EXISTS ix_work_items_assignment_status
    ON work_items (tenant_id, project, assignment_id, status);

CREATE TABLE IF NOT EXISTS work_item_dependencies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    project TEXT NOT NULL,
    goal_id UUID NOT NULL,
    work_item_id UUID NOT NULL,
    depends_on_id UUID NOT NULL,
    dependency_type TEXT NOT NULL DEFAULT 'blocks' CHECK (dependency_type IN ('blocks','requires')),
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT ck_work_item_dependency_not_self CHECK (work_item_id <> depends_on_id),
    CONSTRAINT fk_work_item_dependency_source FOREIGN KEY (work_item_id, tenant_id, project, goal_id)
        REFERENCES work_items(id, tenant_id, project, goal_id) ON DELETE CASCADE,
    CONSTRAINT fk_work_item_dependency_target FOREIGN KEY (depends_on_id, tenant_id, project, goal_id)
        REFERENCES work_items(id, tenant_id, project, goal_id) ON DELETE RESTRICT,
    UNIQUE (tenant_id, project, work_item_id, depends_on_id)
);

CREATE TABLE IF NOT EXISTS work_item_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    project TEXT NOT NULL,
    goal_id UUID NOT NULL,
    work_item_id UUID NOT NULL,
    evidence_type TEXT NOT NULL CHECK (evidence_type IN
        ('command','test','file','commit','job','acceptance_criterion','api','e2e','rollup')),
    uri TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_hash TEXT,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fk_work_item_evidence_scope FOREIGN KEY (work_item_id, tenant_id, project, goal_id)
        REFERENCES work_items(id, tenant_id, project, goal_id) ON DELETE CASCADE,
    CONSTRAINT ck_work_item_evidence_body CHECK (uri IS NOT NULL OR payload <> '{}'::jsonb)
);
CREATE INDEX IF NOT EXISTS ix_work_item_evidence_item
    ON work_item_evidence (tenant_id, project, work_item_id, created_at DESC);

CREATE TABLE IF NOT EXISTS work_item_change_sets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('goal','milestone','epic','story','task')),
    target_id UUID NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create','update','assign','cancel','execute','accept')),
    base_version BIGINT NOT NULL CHECK (base_version > 0),
    patch JSONB NOT NULL CHECK (jsonb_typeof(patch) = 'array'),
    patch_hash TEXT NOT NULL CHECK (patch_hash ~ '^sha256:[0-9a-f]{64}$'),
    rationale TEXT NOT NULL,
    expected_effect TEXT NOT NULL,
    rollback_plan TEXT NOT NULL,
    risk_tier TEXT NOT NULL CHECK (risk_tier IN ('A0','A1','A2','A3')),
    state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN
        ('draft','pending','approved','executing','executed','rejected','revised','expired','superseded','revoked','failed','retry_pending')),
    approval_request_id UUID REFERENCES agent_permission_requests(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
    requested_by UUID NOT NULL,
    decided_by UUID,
    decided_at TIMESTAMPTZ,
    execution_key TEXT,
    executed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, idempotency_key),
    UNIQUE (tenant_id, execution_key)
);

CREATE TABLE IF NOT EXISTS work_item_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    actor_session_id UUID,
    actor_role_key TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id UUID NOT NULL,
    causation_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fk_work_item_event_actor FOREIGN KEY (actor_session_id, tenant_id)
        REFERENCES chat_sessions(id, tenant_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS ix_work_item_events_aggregate
    ON work_item_events (tenant_id, project, aggregate_type, aggregate_id, created_at);

CREATE TABLE IF NOT EXISTS goal_approval_policy_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT,
    policy JSONB NOT NULL,
    policy_hash TEXT NOT NULL CHECK (policy_hash ~ '^sha256:[0-9a-f]{64}$'),
    created_by UUID NOT NULL,
    approved_by UUID,
    mode TEXT NOT NULL DEFAULT 'audit_only' CHECK (mode IN ('audit_only','canary','enabled','retired')),
    effective_at TIMESTAMPTZ,
    previous_version_id UUID REFERENCES goal_approval_policy_versions(id) ON DELETE RESTRICT,
    simulation_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, policy_hash)
);

CREATE TABLE IF NOT EXISTS goal_auto_approval_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    principal_session_id UUID NOT NULL,
    assignment_id UUID NOT NULL,
    goal_id UUID NOT NULL,
    milestone_id UUID,
    epic_id UUID,
    story_id UUID,
    actions TEXT[] NOT NULL CHECK (cardinality(actions) > 0),
    tool_groups TEXT[] NOT NULL DEFAULT '{}',
    max_risk_tier TEXT NOT NULL CHECK (max_risk_tier IN ('A0','A1','A2')),
    environments TEXT[] NOT NULL DEFAULT '{}',
    conditions JSONB NOT NULL DEFAULT '{}'::jsonb,
    max_executions INTEGER NOT NULL CHECK (max_executions >= 0),
    used_executions INTEGER NOT NULL DEFAULT 0 CHECK (used_executions >= 0 AND used_executions <= max_executions),
    max_files INTEGER NOT NULL DEFAULT 0 CHECK (max_files >= 0),
    max_rows BIGINT NOT NULL DEFAULT 0 CHECK (max_rows >= 0),
    max_cost_usd NUMERIC(14,4) NOT NULL DEFAULT 0 CHECK (max_cost_usd >= 0),
    max_parallel INTEGER NOT NULL DEFAULT 1 CHECK (max_parallel > 0),
    max_duration_seconds INTEGER NOT NULL DEFAULT 0 CHECK (max_duration_seconds >= 0),
    valid_from TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    idle_timeout_seconds INTEGER CHECK (idle_timeout_seconds IS NULL OR idle_timeout_seconds > 0),
    delegation_depth INTEGER NOT NULL DEFAULT 0 CHECK (delegation_depth IN (0,1)),
    parent_grant_id UUID REFERENCES goal_auto_approval_grants(id) ON DELETE RESTRICT,
    policy_version UUID NOT NULL REFERENCES goal_approval_policy_versions(id) ON DELETE RESTRICT,
    grant_version INTEGER NOT NULL DEFAULT 1 CHECK (grant_version > 0),
    scope_hash TEXT NOT NULL CHECK (scope_hash ~ '^sha256:[0-9a-f]{64}$'),
    revocation_strategy TEXT NOT NULL CHECK (revocation_strategy IN ('cancel_now','finish_current','compensate')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','stale','expired','superseded')),
    requested_by UUID NOT NULL,
    issued_by UUID NOT NULL,
    approved_by UUID NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    revoked_by UUID,
    revoked_at TIMESTAMPTZ,
    revoke_reason TEXT,
    CONSTRAINT ck_goal_grant_window CHECK (expires_at > valid_from),
    CONSTRAINT ck_goal_grant_no_self_issue CHECK (issued_by <> principal_session_id AND approved_by <> principal_session_id),
    CONSTRAINT ck_goal_grant_no_production CHECK (NOT ('production' = ANY(environments))),
    CONSTRAINT ck_goal_grant_no_mandatory_human_action CHECK
        (NOT (actions && ARRAY['production_deploy','routing_cutover','database_schema','bulk_data_change',
                               'financial','secret','security_policy','destructive']::TEXT[])),
    CONSTRAINT fk_goal_grant_principal FOREIGN KEY (principal_session_id, tenant_id)
        REFERENCES chat_sessions(id, tenant_id) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_grant_assignment FOREIGN KEY (assignment_id, tenant_id, project)
        REFERENCES project_role_assignments(id, tenant_id, project) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_grant_goal FOREIGN KEY (goal_id, tenant_id, project)
        REFERENCES goals(id, tenant_id, project) ON DELETE RESTRICT,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, id, grant_version)
);
CREATE INDEX IF NOT EXISTS ix_goal_grants_principal_status
    ON goal_auto_approval_grants (tenant_id, principal_session_id, status);

CREATE TABLE IF NOT EXISTS goal_auto_approval_uses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    decision_id UUID NOT NULL,
    execution_key TEXT NOT NULL,
    grant_id UUID NOT NULL,
    grant_version INTEGER NOT NULL,
    input_hash TEXT NOT NULL CHECK (input_hash ~ '^sha256:[0-9a-f]{64}$'),
    target_type TEXT NOT NULL CHECK (target_type IN ('goal','milestone','epic','story','task')),
    target_id UUID NOT NULL,
    target_version BIGINT NOT NULL CHECK (target_version > 0),
    patch_hash TEXT CHECK (patch_hash IS NULL OR patch_hash ~ '^sha256:[0-9a-f]{64}$'),
    action TEXT NOT NULL,
    budget_delta JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('reserved','executing','completed','failed','released','manual_reconciliation')),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    executing_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    correlation_id UUID NOT NULL,
    CONSTRAINT fk_goal_auto_use_grant FOREIGN KEY (tenant_id, grant_id, grant_version)
        REFERENCES goal_auto_approval_grants(tenant_id, id, grant_version) ON DELETE RESTRICT,
    UNIQUE (tenant_id, execution_key),
    UNIQUE (tenant_id, decision_id)
);

-- Serialize hierarchy mutations within a tenant/project/goal so concurrent
-- inserts cannot both pass a cycle check.
CREATE OR REPLACE FUNCTION aads_validate_work_item_hierarchy() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE v_parent work_items%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text || ':' || NEW.project || ':' || NEW.goal_id::text, 0));
    IF NEW.parent_id IS NULL THEN RETURN NEW; END IF;
    SELECT * INTO v_parent FROM work_items WHERE id = NEW.parent_id FOR KEY SHARE;
    IF NOT FOUND OR v_parent.tenant_id <> NEW.tenant_id OR v_parent.project <> NEW.project
       OR v_parent.goal_id <> NEW.goal_id OR v_parent.milestone_id <> NEW.milestone_id THEN
        RAISE EXCEPTION 'parent scope mismatch' USING ERRCODE = '23514';
    END IF;
    IF (NEW.type = 'story' AND v_parent.type <> 'epic')
       OR (NEW.type = 'task' AND v_parent.type <> 'story') OR NEW.type = 'epic' THEN
        RAISE EXCEPTION 'invalid parent type' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (
        WITH RECURSIVE ancestors(id, parent_id) AS (
            SELECT id, parent_id FROM work_items WHERE id = NEW.parent_id
            UNION ALL SELECT w.id, w.parent_id FROM work_items w JOIN ancestors a ON w.id = a.parent_id
        ) SELECT 1 FROM ancestors WHERE id = NEW.id
    ) THEN RAISE EXCEPTION 'work item parent cycle' USING ERRCODE = '23514'; END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_work_item_hierarchy ON work_items;
CREATE TRIGGER trg_work_item_hierarchy BEFORE INSERT OR UPDATE OF parent_id,type,tenant_id,project,goal_id,milestone_id
    ON work_items FOR EACH ROW EXECUTE FUNCTION aads_validate_work_item_hierarchy();

CREATE OR REPLACE FUNCTION aads_validate_work_item_dependency() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text || ':' || NEW.project || ':' || NEW.goal_id::text, 1));
    IF EXISTS (
        WITH RECURSIVE downstream(id) AS (
            SELECT depends_on_id FROM work_item_dependencies WHERE work_item_id = NEW.depends_on_id
            UNION SELECT d.depends_on_id FROM work_item_dependencies d JOIN downstream x ON d.work_item_id = x.id
        ) SELECT 1 FROM downstream WHERE id = NEW.work_item_id
    ) THEN RAISE EXCEPTION 'work item dependency cycle' USING ERRCODE = '23514'; END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_work_item_dependency ON work_item_dependencies;
CREATE TRIGGER trg_work_item_dependency BEFORE INSERT OR UPDATE OF work_item_id,depends_on_id,tenant_id,project,goal_id
    ON work_item_dependencies FOR EACH ROW EXECUTE FUNCTION aads_validate_work_item_dependency();

CREATE OR REPLACE FUNCTION aads_forbid_append_only_mutation() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000'; END $$;

CREATE OR REPLACE FUNCTION aads_guard_change_set_immutability() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state NOT IN ('draft','revised') AND
       (NEW.tenant_id, NEW.project, NEW.target_type, NEW.target_id, NEW.action,
        NEW.base_version, NEW.patch, NEW.patch_hash, NEW.risk_tier,
        NEW.idempotency_key, NEW.requested_by)
       IS DISTINCT FROM
       (OLD.tenant_id, OLD.project, OLD.target_type, OLD.target_id, OLD.action,
        OLD.base_version, OLD.patch, OLD.patch_hash, OLD.risk_tier,
        OLD.idempotency_key, OLD.requested_by) THEN
        RAISE EXCEPTION 'submitted change set content is immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION aads_guard_active_grant() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'active' AND
       (to_jsonb(NEW) - ARRAY['status','used_executions','revoked_by','revoked_at','revoke_reason'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','used_executions','revoked_by','revoked_at','revoke_reason']) THEN
        RAISE EXCEPTION 'active grant scope is immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_work_item_change_set_immutable ON work_item_change_sets;
CREATE TRIGGER trg_work_item_change_set_immutable BEFORE UPDATE ON work_item_change_sets
    FOR EACH ROW EXECUTE FUNCTION aads_guard_change_set_immutability();
DROP TRIGGER IF EXISTS trg_goal_active_grant_immutable ON goal_auto_approval_grants;
CREATE TRIGGER trg_goal_active_grant_immutable BEFORE UPDATE ON goal_auto_approval_grants
    FOR EACH ROW EXECUTE FUNCTION aads_guard_active_grant();
DROP TRIGGER IF EXISTS trg_work_item_events_append_only ON work_item_events;
CREATE TRIGGER trg_work_item_events_append_only BEFORE UPDATE OR DELETE ON work_item_events
    FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();
DROP TRIGGER IF EXISTS trg_goal_auto_uses_append_only ON goal_auto_approval_uses;
CREATE TRIGGER trg_goal_auto_uses_append_only BEFORE DELETE ON goal_auto_approval_uses
    FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();
DROP TRIGGER IF EXISTS trg_goal_policy_versions_immutable ON goal_approval_policy_versions;
CREATE TRIGGER trg_goal_policy_versions_immutable BEFORE UPDATE OR DELETE ON goal_approval_policy_versions
    FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();

COMMIT;
