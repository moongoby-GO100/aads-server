-- 162: Unified deployment components and release manifests.
-- Additive/idempotent only. Existing deploy_runs history remains untouched.

ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS component TEXT NOT NULL DEFAULT 'api';
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS deploy_type TEXT NOT NULL DEFAULT 'api_bluegreen';
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS target_env TEXT NOT NULL DEFAULT 'production';
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS release_title TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS release_summary TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS rollback_plan TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS approval_policy TEXT NOT NULL DEFAULT 'auto_if_green';

CREATE INDEX IF NOT EXISTS idx_deploy_runs_component_queue
    ON deploy_runs (project, component, target_env, status, phase, queue_position, created_at)
    WHERE status IN ('queued', 'awaiting_approval');

CREATE TABLE IF NOT EXISTS deploy_components (
    id BIGSERIAL PRIMARY KEY,
    deploy_run_id BIGINT NOT NULL REFERENCES deploy_runs(id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    component TEXT NOT NULL,
    deploy_type TEXT NOT NULL,
    release_sha TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    phase TEXT NOT NULL DEFAULT 'queued',
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms BIGINT,
    health_url TEXT,
    route_url TEXT,
    image_digest TEXT,
    standby_digest TEXT,
    log_path TEXT,
    error_summary TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deploy_components_latest
    ON deploy_components (project, component, updated_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_deploy_components_active
    ON deploy_components (project, component, status, updated_at DESC)
    WHERE status IN ('queued', 'awaiting_approval', 'running', 'verifying', 'syncing_standby');

CREATE TABLE IF NOT EXISTS deploy_release_manifests (
    id BIGSERIAL PRIMARY KEY,
    deploy_run_id BIGINT NOT NULL REFERENCES deploy_runs(id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    component TEXT NOT NULL DEFAULT 'api',
    target_env TEXT NOT NULL DEFAULT 'production',
    release_sha TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    changed_files JSONB NOT NULL DEFAULT '[]'::jsonb,
    tests JSONB NOT NULL DEFAULT '[]'::jsonb,
    commits JSONB NOT NULL DEFAULT '[]'::jsonb,
    risk_flags JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deploy_release_manifests_run
    ON deploy_release_manifests (deploy_run_id, component);

CREATE TABLE IF NOT EXISTS deploy_locks (
    project TEXT NOT NULL,
    component TEXT NOT NULL,
    target_env TEXT NOT NULL DEFAULT 'production',
    deploy_run_id BIGINT,
    owner_instance TEXT,
    owner_epoch TEXT,
    lease_expires_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(project, component, target_env)
);

COMMENT ON COLUMN deploy_runs.component IS 'Deploy target component such as api, dashboard, frontend, backend, docs, db, config, worker, or agent';
COMMENT ON COLUMN deploy_runs.deploy_type IS 'Adapter/deploy strategy used by the deploy coordinator';
COMMENT ON COLUMN deploy_runs.target_env IS 'Deployment environment, normally production';
COMMENT ON TABLE deploy_components IS 'Component-level deployment ledger derived from deploy_runs';
COMMENT ON TABLE deploy_release_manifests IS 'Non-secret release metadata shown in Ops and chat artifacts';
COMMENT ON TABLE deploy_locks IS 'DB lease row for project/component deployment serialization';
