-- 161: Track commit/push/deploy requests in the common ops deploy ledger.
-- Additive/idempotent only. Existing deploy_runs history remains untouched.

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

CREATE INDEX IF NOT EXISTS idx_deploy_runs_request_queue
    ON deploy_runs (project, status, phase, queue_position, created_at)
    WHERE status IN ('queued', 'awaiting_approval');

CREATE INDEX IF NOT EXISTS idx_deploy_runs_component_queue
    ON deploy_runs (project, component, target_env, status, phase, queue_position, created_at)
    WHERE status IN ('queued', 'awaiting_approval');

COMMENT ON COLUMN deploy_runs.requested_by IS 'Actor or system that requested this deployment';
COMMENT ON COLUMN deploy_runs.request_source IS 'Source path/tool/API that queued the deployment request';
COMMENT ON COLUMN deploy_runs.commit_status IS 'Commit gate state at deploy request time';
COMMENT ON COLUMN deploy_runs.push_status IS 'Push gate state at deploy request time';
COMMENT ON COLUMN deploy_runs.auto_start IS 'Whether the queue worker may start this deployment automatically';
COMMENT ON COLUMN deploy_runs.request_payload IS 'Non-secret deploy request metadata used by ops automation';
COMMENT ON COLUMN deploy_runs.requested_at IS 'Timestamp when the deployment was requested';
COMMENT ON COLUMN deploy_runs.component IS 'Deploy target component such as api, dashboard, frontend, backend, docs, db, config, worker, or agent';
COMMENT ON COLUMN deploy_runs.deploy_type IS 'Adapter/deploy strategy used by the deploy coordinator';
COMMENT ON COLUMN deploy_runs.target_env IS 'Deployment environment, normally production';
