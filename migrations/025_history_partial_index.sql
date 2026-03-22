-- is_compacted=false 메시지만 필터링하는 부분 인덱스
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_messages_session_not_compacted
ON chat_messages (session_id, created_at)
WHERE is_compacted = false;
