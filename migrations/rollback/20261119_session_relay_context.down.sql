-- 대상: session_relay.context 제거(저장된 context 는 영구 삭제된다).
-- 조건: context 미사용 코드로 먼저 되돌리고 아래 백업의 성공과 복원 가능 여부를 확인한 뒤 수동 실행한다.
-- 실행 전 백업 명령: pg_dump --table=public.session_relay --data-only --file=/root/aads/backups/session_relay_before_context_down.sql "$DATABASE_URL"
-- 금지: 백업 없이 실행하거나 context 사용 코드가 동작하는 동안 실행하지 않는다.
-- 롤백 순서: 코드 복귀 → 백업 → 아래 DROP. 기존 동명 열이었다면 삭제하지 않는다.
ALTER TABLE session_relay DROP COLUMN IF EXISTS context;
