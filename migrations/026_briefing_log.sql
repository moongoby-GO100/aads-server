-- 026: CEO 프로액티브 브리핑 로그 테이블
-- 마지막 브리핑 시간을 기록하여 "이후 발생한" 이벤트만 필터링

CREATE TABLE IF NOT EXISTS briefing_log (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(50) NOT NULL DEFAULT 'ceo',
    briefed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_briefing_log_user_at
    ON briefing_log (user_id, briefed_at DESC);
