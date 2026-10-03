-- 세션별 능동 추론 강도(effort). 멱등: 여러 번 실행해도 안전하다.
-- app/main.py 시작 시 자동 마이그레이션과 같은 컬럼을 만든다(수동 적용용 정본).
-- 롤백: migrations/rollback/20261003_chat_session_effort.down.sql
BEGIN;

ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS effort_mode VARCHAR(10) NOT NULL DEFAULT 'auto';
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS effort_manual VARCHAR(10) DEFAULT NULL;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS effort_pending JSONB DEFAULT NULL;
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS effort_updated_at TIMESTAMPTZ DEFAULT NULL;

ALTER TABLE chat_turn_executions ADD COLUMN IF NOT EXISTS effort_status JSONB DEFAULT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chat_sessions_effort_mode_chk') THEN
        ALTER TABLE chat_sessions
            ADD CONSTRAINT chat_sessions_effort_mode_chk CHECK (effort_mode IN ('auto', 'manual'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chat_sessions_effort_manual_chk') THEN
        ALTER TABLE chat_sessions
            ADD CONSTRAINT chat_sessions_effort_manual_chk
            CHECK (effort_manual IS NULL OR effort_manual IN ('medium', 'high', 'xhigh'));
    END IF;
END $$;

COMMIT;
