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
COMMENT ON TABLE deploy_phase_events IS 'Append-only deployment phase timeline';
COMMENT ON VIEW deploy_recent_durations IS 'Recent measured deployment duration by project (90-day window)';
