-- AAG v1.1 V11-3: stable-key baselines and rule lifecycle.
-- Additive and repeatable. Legacy count baselines remain readable during rollout.

CREATE TABLE IF NOT EXISTS aag_baselines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    governance_scope TEXT NOT NULL DEFAULT 'default',
    snapshot_id UUID NOT NULL REFERENCES aag_graph_snapshots_v2(id),
    stable_key_version TEXT NOT NULL,
    key_set_digest CHAR(64) NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed','approved','superseded','rejected')),
    proposer_id TEXT NOT NULL,
    approver_id TEXT,
    reason TEXT NOT NULL,
    proposed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    superseded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (approver_id IS NULL OR approver_id <> proposer_id),
    CHECK (status <> 'approved' OR (approver_id IS NOT NULL AND approved_at IS NOT NULL)),
    UNIQUE (project, repository_id, target_ref, governance_scope, key_set_digest)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_aag_baselines_one_approved_scope
    ON aag_baselines(project, repository_id, target_ref, governance_scope)
    WHERE status = 'approved';

CREATE TABLE IF NOT EXISTS aag_baseline_findings (
    baseline_id UUID NOT NULL REFERENCES aag_baselines(id),
    stable_finding_key TEXT NOT NULL,
    stable_key_version TEXT NOT NULL,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    finding JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (baseline_id, stable_finding_key)
);

CREATE TABLE IF NOT EXISTS aag_rule_lifecycle (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    rule TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'warn_only' CHECK (mode IN ('warn_only','enforced')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','rejected')),
    observation_count INTEGER NOT NULL DEFAULT 0 CHECK (observation_count >= 0),
    fixture_digest CHAR(64),
    proposer_id TEXT NOT NULL,
    approver_id TEXT,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    superseded_at TIMESTAMPTZ,
    UNIQUE (project, rule, rule_version),
    CHECK (approver_id IS NULL OR approver_id <> proposer_id),
    CHECK (mode <> 'enforced' OR (
        approver_id IS NOT NULL AND approved_at IS NOT NULL
        AND observation_count >= 2 AND fixture_digest IS NOT NULL
    ))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_aag_rule_lifecycle_active
    ON aag_rule_lifecycle(project, rule) WHERE status = 'active';

CREATE OR REPLACE FUNCTION aag_protect_approved_baseline()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.status = 'approved' THEN
        RAISE EXCEPTION 'approved AAG baseline is immutable';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.status = 'approved' AND (
        NEW.project IS DISTINCT FROM OLD.project OR
        NEW.repository_id IS DISTINCT FROM OLD.repository_id OR
        NEW.target_ref IS DISTINCT FROM OLD.target_ref OR
        NEW.governance_scope IS DISTINCT FROM OLD.governance_scope OR
        NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id OR
        NEW.stable_key_version IS DISTINCT FROM OLD.stable_key_version OR
        NEW.key_set_digest IS DISTINCT FROM OLD.key_set_digest OR
        NEW.proposer_id IS DISTINCT FROM OLD.proposer_id OR
        NEW.approver_id IS DISTINCT FROM OLD.approver_id OR
        NEW.reason IS DISTINCT FROM OLD.reason OR
        NEW.approved_at IS DISTINCT FROM OLD.approved_at OR
        NEW.status NOT IN ('approved','superseded')
    ) THEN
        RAISE EXCEPTION 'approved AAG baseline identity is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_aag_protect_approved_baseline ON aag_baselines;
CREATE TRIGGER trg_aag_protect_approved_baseline
    BEFORE UPDATE OR DELETE ON aag_baselines
    FOR EACH ROW EXECUTE FUNCTION aag_protect_approved_baseline();

CREATE OR REPLACE FUNCTION aag_protect_approved_baseline_findings()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE baseline_status TEXT;
BEGIN
    SELECT status INTO baseline_status FROM aag_baselines
     WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.baseline_id ELSE NEW.baseline_id END;
    IF baseline_status IN ('approved','superseded') THEN
        RAISE EXCEPTION 'approved AAG baseline findings are immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_aag_protect_approved_baseline_findings ON aag_baseline_findings;
CREATE TRIGGER trg_aag_protect_approved_baseline_findings
    BEFORE INSERT OR UPDATE OR DELETE ON aag_baseline_findings
    FOR EACH ROW EXECUTE FUNCTION aag_protect_approved_baseline_findings();

CREATE OR REPLACE FUNCTION aag_protect_enforced_rule()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND OLD.mode = 'enforced' THEN
        RAISE EXCEPTION 'enforced AAG rule lifecycle is immutable';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.mode = 'enforced' AND (
        NEW.project IS DISTINCT FROM OLD.project OR NEW.rule IS DISTINCT FROM OLD.rule OR
        NEW.rule_version IS DISTINCT FROM OLD.rule_version OR
        NEW.mode IS DISTINCT FROM OLD.mode OR NEW.proposer_id IS DISTINCT FROM OLD.proposer_id OR
        NEW.approver_id IS DISTINCT FROM OLD.approver_id OR
        NEW.fixture_digest IS DISTINCT FROM OLD.fixture_digest OR
        NEW.observation_count IS DISTINCT FROM OLD.observation_count OR
        NEW.reason IS DISTINCT FROM OLD.reason OR NEW.status NOT IN ('active','superseded')
    ) THEN
        RAISE EXCEPTION 'enforced AAG rule lifecycle is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_aag_protect_enforced_rule ON aag_rule_lifecycle;
CREATE TRIGGER trg_aag_protect_enforced_rule
    BEFORE UPDATE OR DELETE ON aag_rule_lifecycle
    FOR EACH ROW EXECUTE FUNCTION aag_protect_enforced_rule();

COMMENT ON TABLE aag_baselines IS
    'Approved stable-key sets; approved identity and membership are immutable';
COMMENT ON TABLE aag_rule_lifecycle IS
    'New rules start warn_only and require observations, fixture evidence and independent approval before enforcement';
