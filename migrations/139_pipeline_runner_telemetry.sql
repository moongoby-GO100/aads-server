-- 139: Pipeline Runner telemetry and model performance statistics.
-- Date: 2026-08-31
--
-- Goal:
-- - Preserve model attempts, review results, approval timestamps, and terminal
--   timings as first-class operational data.
-- - Make model speed/completion statistics queryable without parsing logs.

ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS review_verdict TEXT,
    ADD COLUMN IF NOT EXISTS review_score NUMERIC(5,3),
    ADD COLUMN IF NOT EXISTS review_flag_category TEXT,
    ADD COLUMN IF NOT EXISTS review_needs_retry BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS approval_requested_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deployed_at TIMESTAMPTZ;

UPDATE pipeline_jobs
SET completed_at = updated_at
WHERE completed_at IS NULL
  AND status IN ('done', 'error', 'cancelled', 'rejected_done');

UPDATE pipeline_jobs pj
SET
    review_verdict = cr.verdict,
    review_score = cr.score,
    review_flag_category = cr.flag_category,
    review_needs_retry = COALESCE(cr.needs_retry, FALSE)
FROM (
    SELECT DISTINCT ON (job_id)
        job_id,
        verdict,
        score,
        flag_category,
        needs_retry
    FROM code_reviews
    ORDER BY job_id, created_at DESC
) cr
WHERE pj.job_id = cr.job_id
  AND pj.review_verdict IS NULL;

CREATE TABLE IF NOT EXISTS pipeline_runner_events (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL,
    tenant_id UUID,
    project TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT,
    phase TEXT,
    model TEXT,
    actual_model TEXT,
    size VARCHAR(10),
    duration_ms INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runner_events_job_observed
    ON pipeline_runner_events (job_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_runner_events_model_observed
    ON pipeline_runner_events (COALESCE(actual_model, model), observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_runner_events_project_type_observed
    ON pipeline_runner_events (project, event_type, observed_at DESC);

CREATE OR REPLACE VIEW pipeline_runner_model_stats AS
SELECT
    COALESCE(NULLIF(actual_model, ''), NULLIF(model, ''), 'unknown') AS model_key,
    COALESCE(NULLIF(size, ''), 'M') AS size,
    COUNT(*)::INTEGER AS total_jobs,
    COUNT(*) FILTER (WHERE status = 'done')::INTEGER AS done_jobs,
    COUNT(*) FILTER (WHERE status = 'awaiting_approval')::INTEGER AS awaiting_approval_jobs,
    COUNT(*) FILTER (WHERE status = 'rejected_done')::INTEGER AS rejected_done_jobs,
    COUNT(*) FILTER (WHERE status = 'error')::INTEGER AS error_jobs,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE status = 'done') / NULLIF(COUNT(*), 0),
        1
    ) AS done_rate_pct,
    ROUND(
        AVG(EXTRACT(EPOCH FROM (COALESCE(completed_at, updated_at) - COALESCE(started_at, created_at))))::numeric,
        1
    ) AS avg_seconds,
    ROUND(
        percentile_cont(0.5) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (COALESCE(completed_at, updated_at) - COALESCE(started_at, created_at)))
        )::numeric,
        1
    ) AS p50_seconds,
    ROUND(
        percentile_cont(0.9) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (COALESCE(completed_at, updated_at) - COALESCE(started_at, created_at)))
        )::numeric,
        1
    ) AS p90_seconds,
    MAX(COALESCE(completed_at, updated_at)) AS last_observed_at
FROM pipeline_jobs
WHERE COALESCE(started_at, created_at) IS NOT NULL
GROUP BY model_key, size;
