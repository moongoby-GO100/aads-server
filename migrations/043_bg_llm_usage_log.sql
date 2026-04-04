-- AADS-204: Background LLM 사용 로그 테이블
-- qwen-turbo 및 폴백(claude-haiku) 호출 성공/실패를 기록하여 품질 모니터링에 활용.

CREATE TABLE IF NOT EXISTS bg_llm_usage_log (
    id BIGSERIAL PRIMARY KEY,
    service_name VARCHAR(60) NOT NULL,
    model VARCHAR(60) NOT NULL DEFAULT 'qwen-turbo',
    success BOOLEAN NOT NULL,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    latency_ms INT DEFAULT 0,
    error_code VARCHAR(40),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bg_llm_usage_created
    ON bg_llm_usage_log(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bg_llm_usage_service
    ON bg_llm_usage_log(service_name, created_at DESC);
