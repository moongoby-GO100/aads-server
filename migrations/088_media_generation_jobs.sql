-- 088_media_generation_jobs.sql
-- Common job table for image, image edit, and asynchronous video generation.

BEGIN;

CREATE TABLE IF NOT EXISTS media_generation_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt TEXT,
    input_refs JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'queued',
    result_uri TEXT,
    result_path TEXT,
    result_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    requested_by TEXT,
    session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT media_generation_jobs_kind_chk
        CHECK (kind IN ('image', 'edit_image', 'video')),
    CONSTRAINT media_generation_jobs_status_chk
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_media_generation_jobs_job_id
    ON media_generation_jobs(job_id);

CREATE INDEX IF NOT EXISTS idx_media_generation_jobs_kind_status
    ON media_generation_jobs(kind, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_media_generation_jobs_session
    ON media_generation_jobs(session_id, created_at DESC)
    WHERE session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_media_generation_jobs_provider_model
    ON media_generation_jobs(provider, model_id, created_at DESC);

COMMENT ON TABLE media_generation_jobs IS
    'Shared media generation job records for image, edit_image, and async video tools.';
COMMENT ON COLUMN media_generation_jobs.job_id IS
    'External stable media job id returned to tools and API callers.';
COMMENT ON COLUMN media_generation_jobs.kind IS
    'Media job kind: image, edit_image, or video.';
COMMENT ON COLUMN media_generation_jobs.input_refs IS
    'Input image/video references, size, masks, source URLs, or provider request metadata.';
COMMENT ON COLUMN media_generation_jobs.result_metadata IS
    'Provider result metadata, download metadata, or structured error code.';

COMMIT;
