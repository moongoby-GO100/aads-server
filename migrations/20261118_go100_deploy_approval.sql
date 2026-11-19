-- GO100 승인 스냅샷 / go100-deploy-approval. Additive; never auto-applied.
-- Rollback: disable GO100 approval route/consumer; retain table and all data.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE IF NOT EXISTS go100_deploy_approval_snapshots (
    id UUID PRIMARY KEY,
    run_id BIGINT NOT NULL UNIQUE REFERENCES deploy_runs(id) ON DELETE RESTRICT,
    approved_by TEXT NOT NULL CHECK (length(approved_by)>0),
    tenant_id UUID NOT NULL,
    source_job_id TEXT NOT NULL,
    source_session_id UUID NOT NULL,
    source_tuple JSONB NOT NULL CHECK (jsonb_typeof(source_tuple)='object'),
    project TEXT NOT NULL CHECK (project='GO100'),
    component TEXT NOT NULL CHECK (component IN ('backend','frontend')),
    lane TEXT NOT NULL,
    release_sha TEXT NOT NULL CHECK (release_sha ~ '^[0-9a-f]{40}$'),
    target_env TEXT NOT NULL CHECK (length(target_env)>0),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL CHECK (expires_at>issued_at),
    schema_version INTEGER NOT NULL CHECK (schema_version=1),
    snapshot_hash TEXT NOT NULL CHECK (snapshot_hash ~ '^[0-9a-f]{64}$'),
    CHECK ((component='backend' AND lane='api_only') OR (component='frontend' AND lane='frontend'))
);
CREATE INDEX IF NOT EXISTS go100_deploy_approval_tenant_issued ON go100_deploy_approval_snapshots(tenant_id, issued_at);
CREATE OR REPLACE FUNCTION go100_deploy_approval_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'GO100 approval snapshots are immutable' USING ERRCODE='55000';
END;
$$;
CREATE OR REPLACE TRIGGER go100_deploy_approval_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
ON go100_deploy_approval_snapshots FOR EACH STATEMENT EXECUTE FUNCTION go100_deploy_approval_immutable();
