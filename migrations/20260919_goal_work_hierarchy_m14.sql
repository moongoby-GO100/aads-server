-- M14 approval execution and bounded capability grants. Additive/idempotent.
BEGIN;

CREATE TABLE IF NOT EXISTS goal_approval_decision_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK (decision IN ('AUTO','NOTIFY','PROJECT_APPROVAL','CEO_APPROVAL','DENY')),
    policy_version UUID REFERENCES goal_approval_policy_versions(id) ON DELETE RESTRICT,
    matched_grant_id UUID REFERENCES goal_auto_approval_grants(id) ON DELETE RESTRICT,
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    input_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    simulated BOOLEAN NOT NULL DEFAULT FALSE,
    shadow_decision TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_goal_approval_decisions_tenant_created
    ON goal_approval_decision_logs(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS goal_workflow_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    change_set_id UUID NOT NULL REFERENCES work_item_change_sets(id) ON DELETE RESTRICT,
    execution_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivering','delivered','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    owner_instance TEXT NOT NULL,
    owner_epoch BIGINT NOT NULL CHECK (owner_epoch > 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    delivered_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (tenant_id, execution_key)
);
CREATE INDEX IF NOT EXISTS ix_goal_workflow_outbox_pending
    ON goal_workflow_outbox(status, available_at) WHERE status IN ('pending','failed');

CREATE TABLE IF NOT EXISTS goal_approval_kill_switches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT,
    goal_id UUID,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    reason TEXT NOT NULL,
    activated_by UUID NOT NULL,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    deactivated_by UUID,
    deactivated_at TIMESTAMPTZ,
    CHECK (project IS NOT NULL OR goal_id IS NULL)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_goal_kill_switch_active_scope
    ON goal_approval_kill_switches(tenant_id, COALESCE(project,''), COALESCE(goal_id,'00000000-0000-0000-0000-000000000000'::uuid))
    WHERE active;

ALTER TABLE work_item_change_sets
    ADD COLUMN IF NOT EXISTS owner_instance TEXT,
    ADD COLUMN IF NOT EXISTS owner_epoch BIGINT,
    ADD COLUMN IF NOT EXISTS last_error TEXT;

ALTER TABLE goal_auto_approval_grants
    ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION aads_stale_assignment_grants() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.active AND NOT NEW.active THEN
        UPDATE goal_auto_approval_grants SET status='stale', revoked_at=clock_timestamp(),
               revoke_reason='stale_assignment'
         WHERE assignment_id=NEW.id AND status='active';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_stale_assignment_grants ON project_role_assignments;
CREATE TRIGGER trg_stale_assignment_grants AFTER UPDATE OF active ON project_role_assignments
FOR EACH ROW EXECUTE FUNCTION aads_stale_assignment_grants();

CREATE OR REPLACE FUNCTION aads_stale_policy_grants() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.mode IN ('canary','enabled') THEN
        UPDATE goal_auto_approval_grants SET status='stale', revoked_at=clock_timestamp(),
               revoke_reason='stale_policy'
         WHERE tenant_id=NEW.tenant_id AND policy_version<>NEW.id AND status='active'
           AND (NEW.project IS NULL OR project=NEW.project);
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_stale_policy_grants ON goal_approval_policy_versions;
CREATE TRIGGER trg_stale_policy_grants AFTER INSERT ON goal_approval_policy_versions
FOR EACH ROW EXECUTE FUNCTION aads_stale_policy_grants();

CREATE OR REPLACE FUNCTION aads_decision_log_append_only() RETURNS TRIGGER
LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'approval decision logs are append-only' USING ERRCODE='55000'; END $$;
DROP TRIGGER IF EXISTS trg_goal_decision_log_append_only ON goal_approval_decision_logs;
CREATE TRIGGER trg_goal_decision_log_append_only BEFORE UPDATE OR DELETE ON goal_approval_decision_logs
FOR EACH ROW EXECUTE FUNCTION aads_decision_log_append_only();

COMMIT;
