-- AADS-207: A/B 테스트 로그 테이블
CREATE TABLE IF NOT EXISTS ab_test_log (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    batch_id VARCHAR(36) NOT NULL,
    test_tier VARCHAR(10),
    test_type VARCHAR(20),
    difficulty_level INT,
    prompt_text TEXT NOT NULL,
    prompt_hash VARCHAR(64),
    model_name VARCHAR(100) NOT NULL,
    response_text TEXT,
    latency_ms INT,
    input_tokens INT,
    output_tokens INT,
    cost_usd NUMERIC(10,6),
    error_message TEXT,
    judge_score NUMERIC(3,1),
    judge_reason TEXT,
    judge_model VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_ab_test_batch ON ab_test_log(batch_id);
CREATE INDEX IF NOT EXISTS idx_ab_test_model ON ab_test_log(model_name, created_at);
