-- 179_chat_messages_stale_empty_sweep_index.sql
-- stale_empty_placeholder 스윕의 chat_messages 풀스캔 제거
--
-- 배경(2026-09-13 실측):
--   chat_service.py:3244 의 30초 주기 스윕은
--     WHERE role='assistant' AND is_hidden=FALSE
--       AND intent IS DISTINCT FROM 'streaming_placeholder'
--       AND length(trim(COALESCE(content,''))) < 50
--       AND created_at < NOW() - INTERVAL '10 minutes'
--   조건을 인덱스 없이 평가한다. chat_messages 는 39,483행 / 1,178MB 라
--   매 주기 테이블 전체를 읽으며, pg_stat_activity 20Hz 표본 888건 중 6%가
--   이 statement, 65%가 그로 인해 유발된 autovacuum ANALYZE 였다.
--   실제 매칭 행은 644건(1.6%)뿐이다.
--
-- 조치: 스윕 조건을 그대로 담은 부분 인덱스. length/trim/coalesce 는 immutable 이라
--       부분 인덱스 술어로 사용할 수 있다.
-- 주의: CREATE INDEX CONCURRENTLY 는 트랜잭션 블록 안에서 실행할 수 없으므로
--       이 파일에는 BEGIN/COMMIT 을 두지 않는다(175와 동일 방식).
-- 롤백: DROP INDEX CONCURRENTLY IF EXISTS idx_chat_messages_stale_empty_sweep;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chat_messages_stale_empty_sweep
    ON chat_messages (created_at)
    WHERE role = 'assistant'
      AND is_hidden = FALSE
      AND length(trim(COALESCE(content, ''))) < 50;

COMMENT ON INDEX idx_chat_messages_stale_empty_sweep IS
    'stale_empty_placeholder 스윕 전용 부분 인덱스 — 30초 주기 풀스캔 제거 (179)';
