-- 메시지별 접속 환경(client_env). 사용자 메시지 전송 시점의 기기 구분·화면 크기·OS·브라우저 계열만 담는다.
-- 원문 User-Agent·IP·기기 모델명은 저장하지 않는다. NULL = 환경 미수집(기존 클라이언트·시스템 메시지).
-- 멱등. app/main.py 시작 시 자동 마이그레이션과 같은 컬럼을 만든다(수동 적용용 정본).
-- 롤백: migrations/rollback/20261022_chat_messages_client_env.down.sql
BEGIN;

ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS client_env JSONB NULL;

COMMIT;
