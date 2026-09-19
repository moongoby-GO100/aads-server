-- W-14c shadow replay, canary promotion, and audited rollback.
-- Additive/idempotent; apply after W-14b.
BEGIN;

CREATE TABLE IF NOT EXISTS goal_policy_simulation_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    candidate_policy_id UUID NOT NULL,
    requested_by UUID NOT NULL,
    sample_count INTEGER NOT NULL CHECK (sample_count > 0),
    changed_count INTEGER NOT NULL CHECK (changed_count >= 0),
    widened_count INTEGER NOT NULL CHECK (widened_count >= 0),
    summary JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (candidate_policy_id,tenant_id)
        REFERENCES goal_approval_policy_versions(id,tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (requested_by,tenant_id)
        REFERENCES chat_sessions(id,tenant_id) ON DELETE RESTRICT,
    UNIQUE (id,tenant_id,project)
);

CREATE TABLE IF NOT EXISTS goal_policy_simulation_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    run_id UUID NOT NULL,
    historical_decision_id UUID NOT NULL,
    decision_input_hash TEXT NOT NULL CHECK (decision_input_hash ~ '^sha256:[0-9a-f]{64}$'),
    baseline_result TEXT NOT NULL CHECK (baseline_result IN
        ('AUTO','APPROVAL_REQUIRED','DENY','NOT_EXECUTABLE')),
    shadow_result TEXT NOT NULL CHECK (shadow_result IN
        ('AUTO','APPROVAL_REQUIRED','DENY','NOT_EXECUTABLE')),
    widened BOOLEAN NOT NULL,
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (run_id,tenant_id,project)
        REFERENCES goal_policy_simulation_runs(id,tenant_id,project) ON DELETE RESTRICT,
    UNIQUE (tenant_id,run_id,historical_decision_id)
);

CREATE TABLE IF NOT EXISTS goal_policy_rollout_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    policy_version_id UUID NOT NULL,
    from_mode TEXT NOT NULL CHECK (from_mode IN ('audit_only','canary','enabled','retired')),
    to_mode TEXT NOT NULL CHECK (to_mode IN ('canary','enabled','retired')),
    simulation_run_id UUID,
    rollback_policy_id UUID,
    decided_by UUID NOT NULL,
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (policy_version_id,tenant_id)
        REFERENCES goal_approval_policy_versions(id,tenant_id) ON DELETE RESTRICT,
    FOREIGN KEY (decided_by,tenant_id)
        REFERENCES chat_sessions(id,tenant_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_goal_policy_simulation_candidate
    ON goal_policy_simulation_runs(tenant_id,project,candidate_policy_id,created_at DESC);
CREATE INDEX IF NOT EXISTS ix_goal_policy_rollout_current
    ON goal_policy_rollout_events(tenant_id,project,policy_version_id,created_at DESC);

-- Policy content remains immutable. Lifecycle metadata may move only through
-- the audited W-14c state machine; DELETE is always forbidden.
CREATE OR REPLACE FUNCTION aads_guard_policy_version_lifecycle() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'goal policy versions are immutable' USING ERRCODE='55000';
    END IF;
    IF (NEW.id,NEW.tenant_id,NEW.project,NEW.policy,NEW.policy_hash,NEW.created_by,NEW.created_at,
        NEW.previous_version_id)
       IS DISTINCT FROM
       (OLD.id,OLD.tenant_id,OLD.project,OLD.policy,OLD.policy_hash,OLD.created_by,OLD.created_at,
        OLD.previous_version_id) THEN
        RAISE EXCEPTION 'goal policy content is immutable' USING ERRCODE='55000';
    END IF;
    IF NEW.mode = OLD.mode THEN
        IF (NEW.approved_by,NEW.effective_at) IS DISTINCT FROM (OLD.approved_by,OLD.effective_at) THEN
            RAISE EXCEPTION 'policy lifecycle fields require a mode transition' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
    END IF;
    IF (OLD.mode,NEW.mode) NOT IN (
        ('audit_only','canary'),('canary','enabled'),('canary','retired'),
        ('enabled','retired'),('retired','enabled')
    ) THEN
        RAISE EXCEPTION 'invalid policy lifecycle transition' USING ERRCODE='23514';
    END IF;
    IF NEW.approved_by IS NULL OR NEW.effective_at IS NULL
       OR COALESCE((NEW.simulation_result->>'sample_count')::INTEGER,0) <= 0
       OR COALESCE((NEW.simulation_result->>'widened_count')::INTEGER,1) <> 0 THEN
        RAISE EXCEPTION 'policy promotion requires a non-widening simulation' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_goal_policy_versions_immutable ON goal_approval_policy_versions;
CREATE TRIGGER trg_goal_policy_versions_immutable BEFORE UPDATE OR DELETE ON goal_approval_policy_versions
FOR EACH ROW EXECUTE FUNCTION aads_guard_policy_version_lifecycle();

DROP TRIGGER IF EXISTS trg_goal_policy_simulation_runs_append_only ON goal_policy_simulation_runs;
CREATE TRIGGER trg_goal_policy_simulation_runs_append_only BEFORE UPDATE OR DELETE ON goal_policy_simulation_runs
FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();
DROP TRIGGER IF EXISTS trg_goal_policy_simulation_results_append_only ON goal_policy_simulation_results;
CREATE TRIGGER trg_goal_policy_simulation_results_append_only BEFORE UPDATE OR DELETE ON goal_policy_simulation_results
FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();
DROP TRIGGER IF EXISTS trg_goal_policy_rollout_events_append_only ON goal_policy_rollout_events;
CREATE TRIGGER trg_goal_policy_rollout_events_append_only BEFORE UPDATE OR DELETE ON goal_policy_rollout_events
FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();

ALTER TABLE goal_policy_simulation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE goal_policy_simulation_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE goal_policy_simulation_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE goal_policy_simulation_results FORCE ROW LEVEL SECURITY;
ALTER TABLE goal_policy_rollout_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE goal_policy_rollout_events FORCE ROW LEVEL SECURITY;

DO $$
DECLARE table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'goal_policy_simulation_runs','goal_policy_simulation_results','goal_policy_rollout_events'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname=current_schema()
                       AND tablename=table_name AND policyname='tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = aads_current_tenant_id()) WITH CHECK (tenant_id = aads_current_tenant_id())', table_name);
        END IF;
    END LOOP;
END $$;

COMMIT;
