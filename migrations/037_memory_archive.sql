-- 037: memory_archive 테이블 (메모리 삭제 시 백업용)
-- AADS 메모리 진화 모니터링 대시보드

CREATE TABLE IF NOT EXISTS memory_archive (
    id SERIAL PRIMARY KEY,
    source_table VARCHAR(50) NOT NULL,
    source_id INTEGER NOT NULL,
    category VARCHAR(100),
    key VARCHAR(500),
    value TEXT,
    confidence FLOAT,
    project VARCHAR(50),
    archived_at TIMESTAMP DEFAULT NOW(),
    original_created_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_memory_archive_source ON memory_archive(source_table, source_id);
CREATE INDEX IF NOT EXISTS idx_memory_archive_archived_at ON memory_archive(archived_at);
