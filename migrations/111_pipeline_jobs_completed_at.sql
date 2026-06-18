-- 111: Track Pipeline Runner terminal completion time.
-- Date: 2026-06-18
--
-- Keep the schema aligned with the API runner's terminal status persistence.
-- Host shell runners must tolerate deployments where this migration has not run yet.

ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_completed_at
    ON pipeline_jobs (completed_at DESC)
    WHERE completed_at IS NOT NULL;
