-- AADS-CONVERSATIONS-OPT: ILIKE 검색 최적화를 위한 trigram GIN 인덱스
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chat_messages_content_gin
ON chat_messages USING gin (content gin_trgm_ops);
