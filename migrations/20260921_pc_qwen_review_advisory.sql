-- Bounded, non-authoritative PC Qwen shadow reviews.
--
-- The authoritative runner chain remains runner_model_config.AI_REVIEW and
-- CLI-only.  These rows exist solely to collect availability/quality evidence
-- before any future promotion decision.
BEGIN;

CREATE TABLE IF NOT EXISTS runner_review_advisory_config (
    config_key TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    max_samples INTEGER NOT NULL CHECK (max_samples BETWEEN 1 AND 1000),
    timeout_seconds INTEGER NOT NULL CHECK (timeout_seconds BETWEEN 15 AND 300),
    promotion_min_agreement NUMERIC(5,4) NOT NULL DEFAULT 0.8000,
    promotion_max_critical_misses INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS code_review_advisories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id TEXT NOT NULL,
    project TEXT NOT NULL,
    model_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    primary_verdict TEXT NOT NULL,
    primary_model TEXT NOT NULL,
    advisory_verdict TEXT,
    advisory_score REAL,
    agrees_with_primary BOOLEAN,
    diff_size INTEGER,
    response JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    UNIQUE (job_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_code_review_advisories_model_status
    ON code_review_advisories (model_id, status, started_at DESC);

INSERT INTO runner_review_advisory_config
    (config_key, model_id, enabled, max_samples, timeout_seconds,
     promotion_min_agreement, promotion_max_critical_misses, updated_by)
VALUES
    ('default', 'pc-qwen38-27b', TRUE, 20, 90, 0.8000, 0,
     'migration-20260921-pc-qwen-review-advisory')
ON CONFLICT (config_key) DO UPDATE
SET model_id = EXCLUDED.model_id,
    enabled = EXCLUDED.enabled,
    max_samples = EXCLUDED.max_samples,
    timeout_seconds = EXCLUDED.timeout_seconds,
    promotion_min_agreement = EXCLUDED.promotion_min_agreement,
    promotion_max_critical_misses = EXCLUDED.promotion_max_critical_misses,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by;

COMMIT;
