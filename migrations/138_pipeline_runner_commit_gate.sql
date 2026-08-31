-- Pipeline Runner approval/deploy commit identity gate.
ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS commit_hash VARCHAR(40);

COMMENT ON COLUMN pipeline_jobs.commit_hash IS
    'Commit SHA created in the isolated runner worktree before awaiting_approval';

CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_commit_hash
    ON pipeline_jobs (commit_hash)
    WHERE commit_hash IS NOT NULL;
