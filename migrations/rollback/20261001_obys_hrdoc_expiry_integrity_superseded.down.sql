-- Rollback: AADS-OBYS-O2-3-HRDOC-BACKEND-20261001
-- 오비서 업무 DB 전용, 수동 적용. AADS DB 에 돌리지 않는다.
--   psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/rollback/20261001_obys_hrdoc_expiry_integrity_superseded.down.sql
--
-- 추가한 4개 컬럼만 제거한다. 행은 지우지 않는다.
-- 새 코드는 같은 값을 metadata(jsonb)에도 함께 쓰므로 expires_at/sha256/stored_path/superseded_by 는
-- 컬럼을 지운 뒤에도 metadata 에 남는다. status='superseded' 인 행도 그대로 남는다.
-- 롤백 후에는 새 코드(app/services/yeoljeong_finance_service.py 의 onboarding_documents UPSERT)를 함께
-- 되돌려야 한다 — 컬럼이 없으면 새 코드의 INSERT 가 실패한다.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_onboarding_documents') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다';
    END IF;
END
$$;

ALTER TABLE yeoljeong_onboarding_documents DROP COLUMN IF EXISTS superseded_by;
ALTER TABLE yeoljeong_onboarding_documents DROP COLUMN IF EXISTS stored_path;
ALTER TABLE yeoljeong_onboarding_documents DROP COLUMN IF EXISTS sha256;
ALTER TABLE yeoljeong_onboarding_documents DROP COLUMN IF EXISTS expires_at;

COMMIT;
