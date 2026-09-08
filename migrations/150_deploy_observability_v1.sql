-- 150: Common deployment observability v1.
-- Additive/idempotent only: no legacy deploy_history rows are rewritten.

CREATE TABLE IF NOT EXISTS deploy_runs (
    id BIGSERIAL PRIMARY KEY,
    project TEXT NOT NULL,
    release_sha TEXT NOT NULL,
    runner_job_id TEXT,
    deploy_history_id INTEGER REFERENCES deploy_history(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    phase TEXT NOT NULL DEFAULT 'queued',
    phase_started_at TIMESTAMPTZ,
    phase_completed_at TIMESTAMPTZ,
    duration_ms BIGINT,
    estimated_remaining_ms BIGINT,
    current_slot TEXT,
    candidate_slot TEXT,
    image_digest TEXT,
    standby_digest TEXT,
    queue_position INTEGER,
    error_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS deploy_pid INTEGER;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS deploy_generation TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS requested_by TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS request_source TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS commit_status TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS push_status TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS auto_start BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS request_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS component TEXT NOT NULL DEFAULT 'api';
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS deploy_type TEXT NOT NULL DEFAULT 'api_bluegreen';
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS target_env TEXT NOT NULL DEFAULT 'production';
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS release_title TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS release_summary TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS rollback_plan TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS approval_policy TEXT NOT NULL DEFAULT 'auto_if_green';

CREATE UNIQUE INDEX IF NOT EXISTS uq_deploy_runs_project_runner_job
    ON deploy_runs (project, runner_job_id)
    WHERE runner_job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_deploy_runs_project_created
    ON deploy_runs (project, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_deploy_runs_active
    ON deploy_runs (status, updated_at DESC)
    WHERE status IN ('queued', 'awaiting_approval', 'running', 'verifying', 'syncing_standby');

CREATE INDEX IF NOT EXISTS idx_deploy_runs_release_sha
    ON deploy_runs (project, release_sha, created_at DESC);

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

CREATE TABLE IF NOT EXISTS deploy_phase_events (
    id BIGSERIAL PRIMARY KEY,
    deploy_run_id BIGINT NOT NULL REFERENCES deploy_runs(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    phase_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    phase_completed_at TIMESTAMPTZ,
    duration_ms BIGINT,
    estimated_remaining_ms BIGINT,
    current_slot TEXT,
    candidate_slot TEXT,
    image_digest TEXT,
    standby_digest TEXT,
    error_summary TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deploy_phase_events_run_time
    ON deploy_phase_events (deploy_run_id, phase_started_at, id);

CREATE OR REPLACE VIEW deploy_recent_durations AS
WITH deploy_run_samples AS (
    SELECT
        project,
        COUNT(*)::INTEGER AS sample_count,
        ROUND(AVG(duration_ms))::BIGINT AS avg_duration_ms,
        ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms))::BIGINT AS p50_duration_ms,
        ROUND(percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_ms))::BIGINT AS p90_duration_ms,
        MAX(phase_completed_at) AS last_completed_at,
        'deploy_runs'::TEXT AS source,
        1 AS source_rank
    FROM deploy_runs
    WHERE status IN ('completed', 'success')
      AND duration_ms IS NOT NULL
      AND phase_completed_at >= NOW() - INTERVAL '90 days'
    GROUP BY project
),
legacy_samples AS (
    SELECT
        project,
        COUNT(*)::INTEGER AS sample_count,
        ROUND(AVG(duration_s) * 1000)::BIGINT AS avg_duration_ms,
        ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s) * 1000)::BIGINT AS p50_duration_ms,
        ROUND(percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_s) * 1000)::BIGINT AS p90_duration_ms,
        MAX(COALESCE(finished_at, created_at)) AS last_completed_at,
        'deploy_history'::TEXT AS source,
        2 AS source_rank
    FROM deploy_history
    WHERE status = 'success'
      AND duration_s IS NOT NULL
      AND created_at >= NOW() - INTERVAL '90 days'
    GROUP BY project
)
SELECT DISTINCT ON (project)
    project,
    sample_count,
    avg_duration_ms,
    p50_duration_ms,
    p90_duration_ms,
    last_completed_at,
    source
FROM (
    SELECT * FROM deploy_run_samples
    UNION ALL
    SELECT * FROM legacy_samples
) samples
ORDER BY project, source_rank;

COMMENT ON TABLE deploy_runs IS 'Cross-project release status, queue, timing, and blue/green digest state';
COMMENT ON COLUMN deploy_runs.component IS 'Deploy target component such as api, dashboard, frontend, backend, docs, db, config, worker, or agent';
COMMENT ON COLUMN deploy_runs.deploy_type IS 'Adapter/deploy strategy used by the deploy coordinator';
COMMENT ON COLUMN deploy_runs.target_env IS 'Deployment environment, normally production';
COMMENT ON TABLE deploy_phase_events IS 'Append-only deployment phase timeline';
COMMENT ON TABLE deploy_components IS 'Component-level deployment ledger derived from deploy_runs';
COMMENT ON TABLE deploy_release_manifests IS 'Non-secret release metadata shown in Ops and chat artifacts';
COMMENT ON TABLE deploy_locks IS 'DB lease row for project/component deployment serialization';
COMMENT ON VIEW deploy_recent_durations IS 'Recent measured deployment duration by project (90-day window)';
