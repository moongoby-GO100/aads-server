-- AAG v1.1 project RBAC, append-only audit, exceptions and outage overrides.
-- Additive only: legacy v1 tables and routes are intentionally untouched.

CREATE TABLE IF NOT EXISTS aag_project_grants (
    project TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('viewer','scanner','proposer','approver','admin')),
    granted_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (project, principal_id, role)
);

CREATE TABLE IF NOT EXISTS aag_scanner_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    name TEXT NOT NULL,
    token_hash CHAR(64) NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS idx_aag_scanner_credentials_project
    ON aag_scanner_credentials(project, expires_at) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS aag_audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    before_state JSONB,
    after_state JSONB,
    reason TEXT NOT NULL,
    correlation_id UUID NOT NULL,
    source TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_aag_audit_events_project_time
    ON aag_audit_events(project, occurred_at DESC, id);

CREATE OR REPLACE FUNCTION reject_aag_audit_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'AAG audit events are append-only';
END;
$$;
DROP TRIGGER IF EXISTS trg_aag_audit_append_only ON aag_audit_events;
CREATE TRIGGER trg_aag_audit_append_only
    BEFORE UPDATE OR DELETE ON aag_audit_events
    FOR EACH ROW EXECUTE FUNCTION reject_aag_audit_mutation();

CREATE TABLE IF NOT EXISTS aag_finding_exceptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    stable_finding_key TEXT NOT NULL,
    proposer_id TEXT NOT NULL,
    approver_id TEXT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','rejected','expired','revoked')),
    expires_at TIMESTAMPTZ NOT NULL,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (approver_id IS NULL OR approver_id <> proposer_id),
    CHECK (status <> 'approved' OR (approver_id IS NOT NULL AND decided_at IS NOT NULL)),
    CHECK (expires_at > created_at)
);
CREATE INDEX IF NOT EXISTS idx_aag_exceptions_active
    ON aag_finding_exceptions(project, stable_finding_key, expires_at)
    WHERE status = 'approved';

CREATE TABLE IF NOT EXISTS aag_central_outage_overrides (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id UUID NOT NULL UNIQUE,
    project TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    requester_id TEXT NOT NULL,
    approver_id TEXT,
    reason TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    previous_healthy_snapshot_id UUID NOT NULL REFERENCES aag_graph_snapshots_v2(id),
    fallback_snapshot_id UUID REFERENCES aag_graph_snapshots_v2(id),
    independent_verifier_id TEXT,
    independent_verification JSONB,
    rollback_plan JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN (
        'requested','approved','active','expired','rolled_back','rejected','revalidated'
    )),
    replay_nonce_hash CHAR(64) NOT NULL UNIQUE,
    approved_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (approver_id IS NULL OR approver_id <> requester_id),
    CHECK (independent_verifier_id IS NULL OR independent_verifier_id <> requester_id),
    CHECK (independent_verifier_id IS NULL OR independent_verifier_id <> approver_id),
    CHECK (expires_at > created_at),
    CHECK (status NOT IN ('approved','active') OR (
        approver_id IS NOT NULL AND approved_at IS NOT NULL
        AND independent_verifier_id IS NOT NULL
        AND independent_verification IS NOT NULL
    ))
);

CREATE TABLE IF NOT EXISTS aag_revalidation_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    override_id UUID NOT NULL UNIQUE REFERENCES aag_central_outage_overrides(id),
    project TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    expected_commit_sha TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','succeeded','failed','cancelled')),
    authoritative_observation_id UUID REFERENCES aag_snapshot_observations(id),
    not_before TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

COMMENT ON TABLE aag_central_outage_overrides IS
    'Fallback continuity approval only; never makes fallback observations authoritative';
