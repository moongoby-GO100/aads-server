-- 롤백 실행 주체: CEO 승인 후 운영 세션. down 전 다음 COPY 백업을 반드시 완료한다.
-- COPY (SELECT id, context FROM session_relay WHERE context IS NOT NULL)
--   TO '/root/aads/backups/session_relay_context_before_rollback.csv' WITH CSV HEADER;
-- schema_migrations.note 의 precheck=absent(적용 전 열 부재 기록)일 때만 열 삭제를 허용한다.
-- 대상: session_relay.context text, nullable, 기본값 없음; 표 전체 재작성 없이 열을 추가한다.
-- 조건: ADD COLUMN IF NOT EXISTS 로 멱등; 적용기 단일 트랜잭션에서 아래 SET LOCAL 제한을 사용한다.
-- SET LOCAL lock_timeout = '5s'; SET LOCAL statement_timeout = '60s'; 잠금 획득 실패 시 전체 롤백한다.
-- 금지: 기존 context 데이터 덮어쓰기, 백업 없는 열 삭제, 승인 게이트 우회 금지.
-- 롤백: context 를 읽고 쓰지 않는 코드로 먼저 되돌린 후 백업하고
-- migrations/rollback/20261119_session_relay_context.down.sql 을 수동 실행한다. API 롤백만으로 열은 제거되지 않는다.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE session_relay ADD COLUMN IF NOT EXISTS context text;
