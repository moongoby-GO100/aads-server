-- 128: Optional runner auth recovery state.
-- Raw scheduler status may remain error while API/task-board layers expose
-- a specific auth recovery display state.
ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS auth_recovery_state TEXT;

ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS auth_recovery_metadata JSONB;

CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_auth_recovery_state
    ON pipeline_jobs (auth_recovery_state)
    WHERE auth_recovery_state IS NOT NULL;
