-- AADS-186E-2: 4계층 영속 메모리 테이블
-- Layer 2: Working Memory (session_notes)
-- Layer 4: Meta Memory (ai_meta_memory)

-- session_notes: 186B에서 생성될 수 있으므로 IF NOT EXISTS
CREATE TABLE IF NOT EXISTS session_notes (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(100),
    summary TEXT NOT NULL,
    key_decisions TEXT[],
    action_items TEXT[],
    unresolved_issues TEXT[],
    projects_discussed VARCHAR(50)[],
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session_notes_session_id ON session_notes (session_id);
CREATE INDEX IF NOT EXISTS idx_session_notes_created_at ON session_notes (created_at DESC);

-- ai_meta_memory: CEO 선호도, 프로젝트 패턴, 알려진 이슈, 결정 이력
CREATE TABLE IF NOT EXISTS ai_meta_memory (
    id SERIAL PRIMARY KEY,
    category VARCHAR(30) NOT NULL,  -- 'ceo_preference' | 'project_pattern' | 'known_issue' | 'decision_history'
    key VARCHAR(100) NOT NULL UNIQUE,
    value JSONB NOT NULL,
    confidence FLOAT DEFAULT 1.0,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_meta_memory_category ON ai_meta_memory (category);
CREATE INDEX IF NOT EXISTS idx_ai_meta_memory_key ON ai_meta_memory (key);
CREATE INDEX IF NOT EXISTS idx_ai_meta_memory_updated_at ON ai_meta_memory (updated_at DESC);
