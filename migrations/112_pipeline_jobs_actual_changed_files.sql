-- 112: Track files actually changed by Pipeline Runner execution.
-- Date: 2026-07-31

ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS actual_changed_files JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_actual_changed_files_gin
    ON pipeline_jobs USING GIN (actual_changed_files);

COMMENT ON COLUMN pipeline_jobs.actual_changed_files IS
    'Files actually changed by the runner, captured from git diff --name-only after execution.';
