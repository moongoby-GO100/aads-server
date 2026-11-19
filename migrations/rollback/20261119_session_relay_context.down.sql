-- 대상: session_relay.context 제거(저장된 context 는 영구 삭제된다).
-- 조건: context 미사용 코드로 먼저 되돌리고 아래 백업의 성공과 복원 가능 여부를 확인한 뒤 수동 실행한다.
-- 실행 전 백업 명령: pg_dump --table=public.session_relay --data-only --file=/root/aads/backups/session_relay_before_context_down.sql "$DATABASE_URL"
-- 금지: 백업 없이 실행하거나 context 사용 코드가 동작하는 동안 실행하지 않는다.
-- 롤백 순서: 코드 복귀 → 백업 → 아래 DROP. 기존 동명 열이었다면 삭제하지 않는다.
-- 실행 주체: CEO 승인 후 운영 세션. COPY 백업 완료 전 down 금지.
-- COPY (SELECT id, context FROM session_relay WHERE context IS NOT NULL)
--   TO '/root/aads/backups/session_relay_context_before_rollback.csv' WITH CSV HEADER;
-- schema_migrations.note 의 precheck=absent 가 없으면 기존 열 보호를 위해 중단한다.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
LOCK TABLE public.session_relay IN ACCESS EXCLUSIVE MODE;
DO $$
DECLARE v_recorded jsonb;
BEGIN
    SELECT note::jsonb INTO STRICT v_recorded FROM public.schema_migrations
    WHERE filename IN ('migrations/20261119_session_relay_context.sql', '20261119_session_relay_context.sql');
    IF v_recorded->>'precheck' IS DISTINCT FROM 'absent'
       OR NOT (v_recorded->'objects' @> '["column public.session_relay.context"]'::jsonb) IS TRUE THEN
        RAISE EXCEPTION '소유권 미입증: precheck absent 없음 — 삭제 중단';
    END IF;
END $$;
ALTER TABLE public.session_relay DROP COLUMN IF EXISTS context;
COMMIT;
