CREATE TABLE IF NOT EXISTS design_reviews (
    id SERIAL PRIMARY KEY,
    task_id VARCHAR(100),
    page_url VARCHAR(500),
    before_path VARCHAR(500),
    after_path VARCHAR(500),
    verdict VARCHAR(50) NOT NULL DEFAULT 'PENDING',
    issues_json JSONB DEFAULT '[]'::jsonb,
    scores_json JSONB DEFAULT '{}'::jsonb,
    reviewer_model VARCHAR(100),
    cost_usd NUMERIC(10,4) DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_design_reviews_task ON design_reviews(task_id);
CREATE INDEX IF NOT EXISTS idx_design_reviews_verdict ON design_reviews(verdict);
