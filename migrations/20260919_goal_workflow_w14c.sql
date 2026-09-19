-- W-14c policy simulation, shadow replay, canary promotion, and rollback audit.
-- Apply after W-14F, W-14a, and W-14b. Additive/idempotent except for replacing
-- the original policy-hash uniqueness with stage-aware immutable revisions.
BEGIN;

ALTER TABLE goal_approval_policy_versions
    DROP CONSTRAINT IF EXISTS goal_approval_policy_versions_tenant_id_policy_hash_key;
CREATE INDEX IF NOT EXISTS ix_goal_policy_versions_hash
    ON goal_approval_policy_versions(tenant_id,policy_hash,created_at DESC);

CREATE TABLE IF NOT EXISTS goal_policy_simulation_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT,
    candidate_policy_version UUID NOT NULL REFERENCES goal_approval_policy_versions(id) ON DELETE RESTRICT,
    baseline_policy_version UUID REFERENCES goal_approval_policy_versions(id) ON DELETE RESTRICT,
    run_mode TEXT NOT NULL CHECK (run_mode IN ('simulate','shadow','historical_replay')),
    input_count INTEGER NOT NULL CHECK (input_count >= 0),
    divergence_count INTEGER NOT NULL CHECK (divergence_count >= 0),
    privilege_expansion_count INTEGER NOT NULL CHECK (privilege_expansion_count >= 0),
    metrics JSONB NOT NULL DEFAULT '{}',
    operational_counts_before JSONB NOT NULL DEFAULT '{}',
    operational_counts_after JSONB NOT NULL DEFAULT '{}',
    masking_policy_version INTEGER NOT NULL DEFAULT 1 CHECK (masking_policy_version > 0),
    requested_by UUID NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_goal_policy_simulation_runs_candidate
    ON goal_policy_simulation_runs(tenant_id,candidate_policy_version,completed_at DESC);

CREATE TABLE IF NOT EXISTS goal_policy_simulation_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    run_id UUID NOT NULL REFERENCES goal_policy_simulation_runs(id) ON DELETE RESTRICT,
    source_decision_id UUID,
    decision_input_hash TEXT NOT NULL CHECK (decision_input_hash ~ '^sha256:[0-9a-f]{64}$'),
    actual_decision TEXT NOT NULL,
    candidate_decision TEXT NOT NULL,
    divergence BOOLEAN NOT NULL,
    privilege_expansion BOOLEAN NOT NULL,
    masked_context JSONB NOT NULL DEFAULT '{}',
    erased JSONB NOT NULL DEFAULT '[]',
    masked JSONB NOT NULL DEFAULT '[]',
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    UNIQUE (tenant_id,run_id,source_decision_id)
);

CREATE TABLE IF NOT EXISTS goal_policy_promotion_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    project TEXT,
    source_policy_version UUID NOT NULL REFERENCES goal_approval_policy_versions(id) ON DELETE RESTRICT,
    resulting_policy_version UUID NOT NULL REFERENCES goal_approval_policy_versions(id) ON DELETE RESTRICT,
    replay_run_id UUID REFERENCES goal_policy_simulation_runs(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL CHECK (event_type IN ('promote','rollback')),
    from_mode TEXT NOT NULL,
    to_mode TEXT NOT NULL,
    rollback_policy_version UUID REFERENCES goal_approval_policy_versions(id) ON DELETE RESTRICT,
    metrics JSONB NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    approved_by UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp()
);
CREATE INDEX IF NOT EXISTS ix_goal_policy_promotion_events_source
    ON goal_policy_promotion_events(tenant_id,source_policy_version,created_at DESC);

DROP TRIGGER IF EXISTS trg_goal_policy_simulation_runs_append_only ON goal_policy_simulation_runs;
CREATE TRIGGER trg_goal_policy_simulation_runs_append_only
BEFORE UPDATE OR DELETE ON goal_policy_simulation_runs
FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();
DROP TRIGGER IF EXISTS trg_goal_policy_simulation_items_append_only ON goal_policy_simulation_items;
CREATE TRIGGER trg_goal_policy_simulation_items_append_only
BEFORE UPDATE OR DELETE ON goal_policy_simulation_items
FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();
DROP TRIGGER IF EXISTS trg_goal_policy_promotion_events_append_only ON goal_policy_promotion_events;
CREATE TRIGGER trg_goal_policy_promotion_events_append_only
BEFORE UPDATE OR DELETE ON goal_policy_promotion_events
FOR EACH ROW EXECUTE FUNCTION aads_forbid_append_only_mutation();

COMMIT;
