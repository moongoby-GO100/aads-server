-- Durable async review requests: persist intent before invoking the reviewer so
-- an HTTP disconnect cannot lose a completed verdict.
CREATE TABLE IF NOT EXISTS code_review_requests (
    request_id UUID PRIMARY KEY,
    job_id TEXT NOT NULL,
    project TEXT NOT NULL,
    diff TEXT NOT NULL,
    instruction TEXT NOT NULL DEFAULT '',
    files_changed JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    verdict TEXT,
    score NUMERIC(5,3),
    feedback JSONB,
    issues JSONB,
    flag_category TEXT,
    failure_stage TEXT,
    needs_retry BOOLEAN NOT NULL DEFAULT FALSE,
    model_used TEXT,
    error_detail TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_code_review_requests_job_created
    ON code_review_requests(job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_code_review_requests_status_updated
    ON code_review_requests(status, updated_at);

ALTER TABLE pipeline_jobs
    ADD COLUMN IF NOT EXISTS review_request_id UUID;


