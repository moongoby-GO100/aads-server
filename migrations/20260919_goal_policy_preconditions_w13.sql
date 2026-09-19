-- W-13 server-computed policy inputs and immutable precondition snapshots.
BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS ux_project_role_assignments_principal_scope
    ON project_role_assignments (id, tenant_id, project, session_id);

ALTER TABLE milestones ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1
    CHECK (version > 0);

CREATE OR REPLACE FUNCTION aads_increment_milestone_version() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    NEW.version := OLD.version + 1;
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER trg_milestone_version
BEFORE UPDATE ON milestones
FOR EACH ROW EXECUTE FUNCTION aads_increment_milestone_version();

CREATE TABLE IF NOT EXISTS goal_precondition_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    goal_id UUID NOT NULL,
    work_item_id UUID NOT NULL,
    snapshot_version BIGINT NOT NULL CHECK (snapshot_version > 0),
    target_version BIGINT NOT NULL CHECK (target_version > 0),
    assignment_id UUID NOT NULL,
    snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    snapshot_hash TEXT NOT NULL CHECK (snapshot_hash ~ '^sha256:[0-9a-f]{64}$'),
    evidence_snapshot_hash TEXT NOT NULL CHECK (evidence_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'),
    computed_by UUID NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    FOREIGN KEY (work_item_id,tenant_id,project,goal_id)
        REFERENCES work_items(id,tenant_id,project,goal_id) ON DELETE RESTRICT,
    FOREIGN KEY (assignment_id,tenant_id,project)
        REFERENCES project_role_assignments(id,tenant_id,project) ON DELETE RESTRICT,
    FOREIGN KEY (computed_by,tenant_id) REFERENCES chat_sessions(id,tenant_id) ON DELETE RESTRICT,
    UNIQUE (tenant_id,work_item_id,snapshot_version),
    UNIQUE (tenant_id,work_item_id,snapshot_version,snapshot_hash)
);

CREATE TABLE IF NOT EXISTS goal_policy_inputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    workspace_kind TEXT NOT NULL CHECK (workspace_kind IN ('project','ceo')),
    work_item_id UUID NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('epic','story','task')),
    target_version BIGINT NOT NULL CHECK (target_version > 0),
    assignment_id UUID NOT NULL,
    principal_session_id UUID NOT NULL,
    coordinator_session_id UUID NOT NULL,
    action TEXT NOT NULL CHECK (btrim(action)<>''),
    patch JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(patch)='array'),
    patch_hash TEXT NOT NULL CHECK (patch_hash ~ '^sha256:[0-9a-f]{64}$'),
    environment TEXT NOT NULL CHECK (environment IN ('dev','staging','production')),
    risk_factors TEXT[] NOT NULL DEFAULT '{}',
    precondition_snapshot_version BIGINT NOT NULL CHECK (precondition_snapshot_version > 0),
    precondition_snapshot_hash TEXT NOT NULL CHECK (precondition_snapshot_hash ~ '^sha256:[0-9a-f]{64}$'),
    policy_version UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    CONSTRAINT fk_goal_policy_input_principal_assignment
        FOREIGN KEY (assignment_id,tenant_id,project,principal_session_id)
        REFERENCES project_role_assignments(id,tenant_id,project,session_id) ON DELETE RESTRICT,
    FOREIGN KEY (principal_session_id,tenant_id) REFERENCES chat_sessions(id,tenant_id) ON DELETE RESTRICT,
    CONSTRAINT fk_goal_policy_input_coordinator_session
        FOREIGN KEY (coordinator_session_id,tenant_id)
        REFERENCES chat_sessions(id,tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (policy_version,tenant_id) REFERENCES goal_approval_policy_versions(id,tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id,work_item_id,precondition_snapshot_version,precondition_snapshot_hash)
        REFERENCES goal_precondition_snapshots
            (tenant_id,work_item_id,snapshot_version,snapshot_hash) ON DELETE RESTRICT
);

ALTER TABLE goal_precondition_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE goal_precondition_snapshots FORCE ROW LEVEL SECURITY;
ALTER TABLE goal_policy_inputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE goal_policy_inputs FORCE ROW LEVEL SECURITY;
DO $$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['goal_precondition_snapshots','goal_policy_inputs'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname=current_schema()
                       AND tablename=table_name AND policyname='tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id=aads_current_tenant_id()) WITH CHECK (tenant_id=aads_current_tenant_id())', table_name);
        END IF;
    END LOOP;
END $$;

COMMIT;
