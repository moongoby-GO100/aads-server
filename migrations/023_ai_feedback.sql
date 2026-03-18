-- AI-to-AI 피드백 시스템 테이블
CREATE TABLE IF NOT EXISTS code_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id TEXT NOT NULL,
    project TEXT NOT NULL,
    verdict TEXT NOT NULL,
    score REAL NOT NULL,
    feedback JSONB DEFAULT '{}',
    diff_size INT,
    review_cycle INT DEFAULT 1,
    model_used TEXT,
    cost NUMERIC(10,6),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS response_critiques (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID,
    message_id UUID,
    verdict TEXT NOT NULL,
    score REAL NOT NULL,
    details JSONB DEFAULT '{}',
    regenerated BOOLEAN DEFAULT FALSE,
    model_used TEXT,
    cost NUMERIC(10,6),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS debate_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID,
    question TEXT NOT NULL,
    intent TEXT,
    perspectives JSONB DEFAULT '[]',
    synthesis TEXT,
    total_cost NUMERIC(10,6),
    duration_ms INT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_code_reviews_job ON code_reviews(job_id);
CREATE INDEX IF NOT EXISTS idx_critiques_session ON response_critiques(session_id);
CREATE INDEX IF NOT EXISTS idx_debates_session ON debate_sessions(session_id);
