-- 턴(execution)별 응답 모드(plan/quality/fast). 재개·재생성·MCP 브리지가 계획만 모드를 이어받는 근거다.
-- 멱등. app/main.py 시작 시 자동 마이그레이션과 같은 컬럼을 만든다(수동 적용용 정본).
-- NULL 은 quality 로 읽는다. 롤백: migrations/rollback/20261021_chat_turn_response_mode.down.sql
BEGIN;

ALTER TABLE chat_turn_executions ADD COLUMN IF NOT EXISTS response_mode VARCHAR(20) DEFAULT NULL;

COMMIT;
