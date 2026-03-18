-- 032_chat_files.sql: 파일 첨부 시스템 Phase 1
CREATE TABLE IF NOT EXISTS chat_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    message_id UUID,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_size BIGINT NOT NULL,
    category TEXT NOT NULL DEFAULT 'attachment',
    uploaded_by TEXT NOT NULL DEFAULT 'user',
    storage_path TEXT NOT NULL,
    thumbnail_path TEXT,
    width INT,
    height INT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_files_session ON chat_files(session_id);
CREATE INDEX IF NOT EXISTS idx_chat_files_message ON chat_files(message_id);
