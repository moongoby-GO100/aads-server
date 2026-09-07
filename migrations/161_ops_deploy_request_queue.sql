-- 161: Track commit/push/deploy requests in the common ops deploy ledger.
-- Additive/idempotent only. Existing deploy_runs history remains untouched.

ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS requested_by TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS request_source TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS commit_status TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS push_status TEXT;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS auto_start BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS request_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE deploy_runs ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_deploy_runs_request_queue
    ON deploy_runs (project, status, phase, queue_position, created_at)
    WHERE status IN ('queued', 'awaiting_approval');

COMMENT ON COLUMN deploy_runs.requested_by IS 'Actor or system that requested this deployment';
COMMENT ON COLUMN deploy_runs.request_source IS 'Source path/tool/API that queued the deployment request';
COMMENT ON COLUMN deploy_runs.commit_status IS 'Commit gate state at deploy request time';
COMMENT ON COLUMN deploy_runs.push_status IS 'Push gate state at deploy request time';
COMMENT ON COLUMN deploy_runs.auto_start IS 'Whether the queue worker may start this deployment automatically';
COMMENT ON COLUMN deploy_runs.request_payload IS 'Non-secret deploy request metadata used by ops automation';
COMMENT ON COLUMN deploy_runs.requested_at IS 'Timestamp when the deployment was requested';
