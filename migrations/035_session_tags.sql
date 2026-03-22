-- 035: 세션 태그 기능 (P1-5)
-- 세션에 태그 컬럼 추가 (TEXT 배열)
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{}';

-- 태그 검색용 GIN 인덱스
CREATE INDEX IF NOT EXISTS idx_session_tags ON chat_sessions USING GIN (tags);
