-- M2: durable, auditable deployment-request batching relationships.
-- Additive and idempotent.  No existing deploy row is rewritten here.

CREATE TABLE IF NOT EXISTS deploy_batch_inclusions (
    id BIGSERIAL PRIMARY KEY,
    representative_run_id BIGINT NOT NULL REFERENCES deploy_runs(id) ON DELETE CASCADE,
    included_run_id BIGINT NOT NULL REFERENCES deploy_runs(id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    component TEXT NOT NULL,
    target_env TEXT NOT NULL DEFAULT 'production',
    included_sha TEXT NOT NULL,
    representative_sha TEXT NOT NULL,
    relationship TEXT NOT NULL,
    compatibility_reason TEXT NOT NULL,
    resolved_by TEXT NOT NULL DEFAULT 'host_git_batcher',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT deploy_batch_inclusions_distinct_run_chk
        CHECK (representative_run_id <> included_run_id),
    CONSTRAINT deploy_batch_inclusions_relationship_chk
        CHECK (relationship IN ('ancestor', 'exact'))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_deploy_batch_inclusions_included
    ON deploy_batch_inclusions (included_run_id);
CREATE INDEX IF NOT EXISTS idx_deploy_batch_inclusions_representative
    ON deploy_batch_inclusions (representative_run_id, created_at, id);

COMMENT ON TABLE deploy_batch_inclusions IS
    'Pre-deploy queue relation: a host-Git-verified request included in one representative release.';
COMMENT ON COLUMN deploy_batch_inclusions.compatibility_reason IS
    'Why the host batcher considered the requests compatible; ambiguous/risky requests are not inserted.';
