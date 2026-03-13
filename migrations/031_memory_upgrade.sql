-- 031_memory_upgrade.sql
-- AADS 12-Feature Memory Upgrade: 3-Tier Memory Architecture
-- memory_facts, tool_results_archive, ceo_interaction_patterns, chat_messages quality columns

-- ─── memory_facts: 핵심사실 저장소 (12기능의 핵심 테이블) ────────────────────
CREATE TABLE IF NOT EXISTS memory_facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES chat_sessions(id) ON DELETE SET NULL,
    workspace_id UUID REFERENCES chat_workspaces(id) ON DELETE SET NULL,
    project VARCHAR(20),
    category VARCHAR(30) NOT NULL,
    subject VARCHAR(300) NOT NULL,
    detail TEXT NOT NULL,
    context_snippet TEXT,
    confidence FLOAT DEFAULT 0.7,
    embedding vector(768),
    referenced_count INT DEFAULT 0,
    last_referenced_at TIMESTAMPTZ,
    superseded_by UUID REFERENCES memory_facts(id),
    related_facts UUID[],
    tags TEXT[],
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_facts_project ON memory_facts(project);
CREATE INDEX IF NOT EXISTS idx_memory_facts_category ON memory_facts(category);
CREATE INDEX IF NOT EXISTS idx_memory_facts_confidence ON memory_facts(confidence DESC);
CREATE INDEX IF NOT EXISTS idx_memory_facts_created ON memory_facts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_facts_session ON memory_facts(session_id);
CREATE INDEX IF NOT EXISTS idx_memory_facts_embedding ON memory_facts USING hnsw (embedding vector_cosine_ops);

-- ─── tool_results_archive: 도구 결과 전문 보관 ────────────────────────────────
CREATE TABLE IF NOT EXISTS tool_results_archive (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES chat_messages(id) ON DELETE CASCADE,
    tool_use_id VARCHAR(100) NOT NULL,
    tool_name VARCHAR(100) NOT NULL,
    input_params JSONB,
    raw_output TEXT NOT NULL,
    output_tokens INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(message_id, tool_use_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_results_archive_tool ON tool_results_archive(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_results_archive_created ON tool_results_archive(created_at DESC);

-- ─── ceo_interaction_patterns: CEO 패턴 학습 (F8) ────────────────────────────
CREATE TABLE IF NOT EXISTS ceo_interaction_patterns (
    id SERIAL PRIMARY KEY,
    pattern_type VARCHAR(30) NOT NULL,
    pattern_key VARCHAR(200) NOT NULL,
    pattern_value JSONB NOT NULL,
    confidence FLOAT DEFAULT 0.5,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(pattern_type, pattern_key)
);

-- ─── self-evaluation columns (F11) ──────────────────────────────────────────
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS quality_score FLOAT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS quality_details JSONB;
