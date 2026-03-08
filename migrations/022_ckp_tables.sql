-- AADS-186B: CKP(Codebase Knowledge Package) 시스템 테이블
-- 생성일: 2026-03-09

CREATE TABLE IF NOT EXISTS ckp_index (
    id SERIAL PRIMARY KEY,
    project VARCHAR(50) NOT NULL,
    file_path TEXT NOT NULL,
    file_type VARCHAR(20),  -- 'claude_md', 'architecture', 'codebase_map', 'dependency_map', 'lessons'
    token_count INTEGER,
    last_scanned_at TIMESTAMPTZ,
    last_commit_sha VARCHAR(40),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (project, file_path)
);

CREATE INDEX IF NOT EXISTS idx_ckp_index_project ON ckp_index(project);
CREATE INDEX IF NOT EXISTS idx_ckp_index_file_type ON ckp_index(project, file_type);

CREATE TABLE IF NOT EXISTS ckp_lessons (
    id SERIAL PRIMARY KEY,
    project VARCHAR(50) NOT NULL,
    category VARCHAR(30),  -- 'bug_fix', 'architecture_decision', 'performance', 'security', 'pattern'
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    related_files TEXT[],  -- 관련 파일 경로 배열
    source_task_id VARCHAR(20),  -- 예: AADS-185
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ckp_lessons_project ON ckp_lessons(project);
CREATE INDEX IF NOT EXISTS idx_ckp_lessons_category ON ckp_lessons(project, category);
CREATE INDEX IF NOT EXISTS idx_ckp_lessons_task ON ckp_lessons(source_task_id);
