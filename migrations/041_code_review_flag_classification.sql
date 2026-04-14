ALTER TABLE code_reviews
    ADD COLUMN IF NOT EXISTS flag_category TEXT,
    ADD COLUMN IF NOT EXISTS failure_stage TEXT,
    ADD COLUMN IF NOT EXISTS needs_retry BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_code_reviews_flag_category
    ON code_reviews(flag_category);
