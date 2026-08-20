-- Migration 123: deploy_history 다운타임 측정 컬럼 추가
-- 배경: 2026-08-20 인시던트 — Blue/Green 동시 force-recreate로 약 26분 다운.
--       재발 방지 P1 항목 — 배포별 다운타임을 자동 측정/기록한다.

-- downtime_seconds: 서비스 중단 시간(초), health check 기반 자동 측정
ALTER TABLE deploy_history ADD COLUMN IF NOT EXISTS downtime_seconds INTEGER DEFAULT 0;
-- finished_at이 없으면 추가
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='deploy_history' AND column_name='finished_at') THEN
        ALTER TABLE deploy_history ADD COLUMN finished_at TIMESTAMPTZ;
    END IF;
END $$;
