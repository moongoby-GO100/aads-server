-- 030: 메모리 사용 추적 컬럼 추가 (AADS 메모리 활용 추적 시스템)
-- ai_observations 테이블에 usage_count, last_used_at 컬럼 추가

ALTER TABLE ai_observations ADD COLUMN IF NOT EXISTS usage_count INTEGER DEFAULT 0;
ALTER TABLE ai_observations ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
