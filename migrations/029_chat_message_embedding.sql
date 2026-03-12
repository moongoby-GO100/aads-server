-- 029: chat_messages 임베딩 컬럼 추가 (시맨틱 검색용)
-- pgvector 0.8.2: ivfflat/hnsw 모두 2000차원 제한 → 768차원 사용
-- Gemini embedding-001 output_dimensionality=768

ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS embedding vector(768);

CREATE INDEX IF NOT EXISTS idx_chat_msg_embedding
    ON chat_messages USING hnsw (embedding vector_cosine_ops);
