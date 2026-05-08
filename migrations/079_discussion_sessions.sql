-- 079: Discussion Orchestrator 세션 테이블
-- 멀티-LLM 토론 결과를 영구 저장

CREATE TABLE IF NOT EXISTS discussion_sessions (
    id                TEXT PRIMARY KEY,
    session_id        UUID,
    topic             TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active',
    mode              TEXT NOT NULL DEFAULT 'manual',
    participants      JSONB NOT NULL DEFAULT '[]'::jsonb,
    current_round     INTEGER NOT NULL DEFAULT 0,
    rounds            JSONB NOT NULL DEFAULT '[]'::jsonb,
    synthesizer_model TEXT DEFAULT 'claude-opus-4-6',
    synthesis         TEXT DEFAULT '',
    budget_usd        NUMERIC(10,4) DEFAULT 10.0,
    total_cost        NUMERIC(10,4) DEFAULT 0.0,
    duration_ms       INTEGER DEFAULT 0,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discussion_sessions_session
    ON discussion_sessions (session_id);
CREATE INDEX IF NOT EXISTS idx_discussion_sessions_status
    ON discussion_sessions (status);
