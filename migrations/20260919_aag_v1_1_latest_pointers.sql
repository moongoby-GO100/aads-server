-- AAG v1.1 V11-2: ref-scoped authoritative heads and atomic latest pointers.
-- Additive only; the legacy aag_graph_snapshots table is intentionally untouched.

CREATE TABLE IF NOT EXISTS aag_ref_heads (
    project TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    governance_scope TEXT NOT NULL DEFAULT 'default',
    head_commit_sha TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL,
    observation_id UUID NOT NULL REFERENCES aag_snapshot_observations(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project, repository_id, target_ref, governance_scope)
);

CREATE TABLE IF NOT EXISTS aag_latest_pointers (
    project TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    governance_scope TEXT NOT NULL DEFAULT 'default',
    snapshot_id UUID NOT NULL REFERENCES aag_graph_snapshots_v2(id),
    observation_id UUID NOT NULL REFERENCES aag_snapshot_observations(id),
    run_id UUID NOT NULL REFERENCES aag_scan_runs(id),
    resolved_commit_sha TEXT NOT NULL,
    expected_target_ref_head_sha TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project, repository_id, target_ref, governance_scope)
);

-- A partially applied V11-2 draft may have created either table first.
ALTER TABLE aag_ref_heads
    ADD COLUMN IF NOT EXISTS governance_scope TEXT NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS generated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS observation_id UUID REFERENCES aag_snapshot_observations(id),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE aag_latest_pointers
    ADD COLUMN IF NOT EXISTS governance_scope TEXT NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES aag_scan_runs(id),
    ADD COLUMN IF NOT EXISTS expected_target_ref_head_sha TEXT,
    ADD COLUMN IF NOT EXISTS generated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Some production hosts applied the first foundation draft before the
-- ref-head columns were added.  Release assets execute only SQL files changed
-- by the new release, so V11-2 must reconcile those additive foundation
-- columns itself instead of assuming the older migration will be replayed.
ALTER TABLE aag_scan_runs
    ADD COLUMN IF NOT EXISTS expected_target_ref_head_sha TEXT;
UPDATE aag_scan_runs
   SET expected_target_ref_head_sha=resolved_commit_sha
 WHERE expected_target_ref_head_sha IS NULL;
ALTER TABLE aag_scan_runs
    ALTER COLUMN expected_target_ref_head_sha SET NOT NULL;

ALTER TABLE aag_snapshot_observations
    ADD COLUMN IF NOT EXISTS expected_target_ref_head_sha TEXT,
    ADD COLUMN IF NOT EXISTS verification_status TEXT;
UPDATE aag_snapshot_observations
   SET expected_target_ref_head_sha=resolved_commit_sha
 WHERE expected_target_ref_head_sha IS NULL;
UPDATE aag_snapshot_observations
   SET verification_status=CASE WHEN authoritative THEN 'verified' ELSE 'unknown' END
 WHERE verification_status IS NULL;
ALTER TABLE aag_snapshot_observations
    ALTER COLUMN expected_target_ref_head_sha SET NOT NULL,
    ALTER COLUMN verification_status SET NOT NULL;

ALTER TABLE aag_snapshot_observations
    DROP CONSTRAINT IF EXISTS aag_snapshot_observations_verification_status_check;
ALTER TABLE aag_snapshot_observations
    ADD CONSTRAINT aag_snapshot_observations_verification_status_check
    CHECK (verification_status IN (
        'verified', 'commit_mismatch', 'out_of_order', 'unknown'
    )) NOT VALID;
ALTER TABLE aag_snapshot_observations
    VALIDATE CONSTRAINT aag_snapshot_observations_verification_status_check;

CREATE INDEX IF NOT EXISTS idx_aag_latest_pointers_snapshot
    ON aag_latest_pointers(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_aag_latest_pointers_verified
    ON aag_latest_pointers(verified_at DESC);

COMMENT ON TABLE aag_ref_heads IS
    'Last authoritative ref/order evidence, isolated by project/repository/ref/scope';
COMMENT ON TABLE aag_latest_pointers IS
    'Authoritative AAG v1.1 latest pointer; only atomic publish transactions may update it';
