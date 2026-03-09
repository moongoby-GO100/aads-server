-- AADS-186E-3: AI 자동 관찰 기록 테이블
-- Layer 4 확장: ai_meta_memory(수동 학습) + ai_observations(자동 관찰)

CREATE TABLE IF NOT EXISTS ai_observations (
    id SERIAL PRIMARY KEY,
    category VARCHAR(30) NOT NULL,  -- 'ceo_preference' | 'project_pattern' | 'recurring_issue' | 'decision' | 'learning'
    key VARCHAR(100) NOT NULL,
    value TEXT NOT NULL,
    confidence FLOAT DEFAULT 0.5,   -- 0.0~1.0, 반복 확인 시 증가
    source_session_id INTEGER,
    last_confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(category, key)
);

CREATE INDEX IF NOT EXISTS idx_ai_observations_category ON ai_observations (category);
CREATE INDEX IF NOT EXISTS idx_ai_observations_confidence ON ai_observations (confidence DESC);
CREATE INDEX IF NOT EXISTS idx_ai_observations_updated_at ON ai_observations (updated_at DESC);
