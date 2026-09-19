-- AAG v1.1 foundation: attempts, immutable content, and freshness observations.
-- Additive only. Legacy aag_graph_snapshots and /api/v1/aag/* remain unchanged.

CREATE TABLE IF NOT EXISTS aag_scan_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    governance_scope TEXT NOT NULL DEFAULT 'default',
    resolved_commit_sha TEXT NOT NULL,
    expected_target_ref_head_sha TEXT NOT NULL,
    input_fingerprint CHAR(64) NOT NULL,
    scanner_version TEXT NOT NULL,
    ruleset_digest CHAR(64) NOT NULL,
    scan_scope_digest CHAR(64) NOT NULL,
    normalization_version TEXT NOT NULL,
    parser_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
    host TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT 'running' CHECK (result IN (
        'running', 'succeeded', 'no_change_success', 'scan_failed',
        'ingest_failed', 'skipped_locked', 'cancelled', 'out_of_order',
        'source_behind'
    )),
    stage_status JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code TEXT,
    error_detail TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aag_scan_runs_scope_started
    ON aag_scan_runs(project, repository_id, target_ref, governance_scope, started_at DESC);

CREATE TABLE IF NOT EXISTS aag_graph_snapshots_v2 (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    content_fingerprint CHAR(64) NOT NULL,
    canonicalization_version TEXT NOT NULL,
    stable_key_version TEXT NOT NULL,
    stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    nodes JSONB NOT NULL DEFAULT '[]'::jsonb,
    edges JSONB NOT NULL DEFAULT '[]'::jsonb,
    findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    unresolved JSONB NOT NULL DEFAULT '[]'::jsonb,
    node_count INTEGER NOT NULL CHECK (node_count >= 0),
    edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
    finding_count INTEGER NOT NULL CHECK (finding_count >= 0),
    generated_at TIMESTAMPTZ NOT NULL,
    publish_status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (publish_status IN ('candidate', 'ready', 'rejected')),
    first_published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project, repository_id, content_fingerprint),
    CHECK ((publish_status = 'ready') = (first_published_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS aag_snapshot_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL UNIQUE REFERENCES aag_scan_runs(id),
    snapshot_id UUID NOT NULL REFERENCES aag_graph_snapshots_v2(id),
    project TEXT NOT NULL,
    repository_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    governance_scope TEXT NOT NULL DEFAULT 'default',
    resolved_commit_sha TEXT NOT NULL,
    expected_target_ref_head_sha TEXT NOT NULL,
    input_fingerprint CHAR(64) NOT NULL,
    content_fingerprint CHAR(64) NOT NULL,
    authoritative BOOLEAN NOT NULL DEFAULT FALSE,
    result TEXT NOT NULL CHECK (result IN ('succeeded', 'no_change_success')),
    verification_status TEXT NOT NULL CHECK (verification_status IN (
        'verified', 'commit_mismatch', 'unknown'
    )),
    verified_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (NOT authoritative OR verification_status = 'verified')
);

-- Compatibility for hosts where the first additive draft was already applied.
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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'aag_snapshot_observations_verification_status_check'
           AND conrelid = 'aag_snapshot_observations'::regclass
    ) THEN
        ALTER TABLE aag_snapshot_observations
            ADD CONSTRAINT aag_snapshot_observations_verification_status_check
            CHECK (verification_status IN ('verified', 'commit_mismatch', 'unknown'));
    END IF;
END;
$$;

UPDATE aag_scan_runs r
   SET result=o.result,
       finished_at=COALESCE(r.finished_at, o.verified_at),
       stage_status='{"scan":"succeeded","ingest":"succeeded"}'::jsonb
  FROM aag_snapshot_observations o
 WHERE o.run_id=r.id AND r.result='running';

CREATE INDEX IF NOT EXISTS idx_aag_observations_scope_verified
    ON aag_snapshot_observations(
        project, repository_id, target_ref, governance_scope, verified_at DESC
    );
CREATE INDEX IF NOT EXISTS idx_aag_observations_snapshot
    ON aag_snapshot_observations(snapshot_id, verified_at DESC);

CREATE OR REPLACE FUNCTION reject_ready_aag_snapshot_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.publish_status = 'ready' THEN
        RAISE EXCEPTION 'ready AAG snapshots are immutable';
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'trg_aag_ready_snapshot_immutable' AND NOT tgisinternal
    ) THEN
        CREATE TRIGGER trg_aag_ready_snapshot_immutable
            BEFORE UPDATE OR DELETE ON aag_graph_snapshots_v2
            FOR EACH ROW EXECUTE FUNCTION reject_ready_aag_snapshot_mutation();
    END IF;
END;
$$;

COMMENT ON TABLE aag_scan_runs IS 'AAG v1.1 every-attempt ledger; failures never overwrite usable snapshots';
COMMENT ON TABLE aag_graph_snapshots_v2 IS 'AAG v1.1 immutable canonical graph content, deduplicated by fingerprint';
COMMENT ON TABLE aag_snapshot_observations IS 'AAG v1.1 run/ref verification events; repeated content refreshes freshness here';
