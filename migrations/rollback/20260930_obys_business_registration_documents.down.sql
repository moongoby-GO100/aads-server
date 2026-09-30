-- Rollback: AADS-OBYS-BIZLICENSE-ORIGINAL-OCR-20260930
-- 오비서 업무 DB 전용, 수동 적용. AADS DB 에 돌리지 않는다.
-- 주의: 등록증 버전 메타데이터(sha256·업로더·시각)가 사라진다. 원본 파일은
-- OBYS_UPLOAD_ROOT/<tenant>/<business 해시>/business_registration/ 에 그대로 남는다.
-- 되돌리기 전에 표를 덤프해 두어라:
--   pg_dump "$OBYS_DATABASE_URL" -t yeoljeong_business_registration_documents > registration_documents.sql

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.yeoljeong_businesses') IS NULL THEN
        RAISE EXCEPTION '오비서 업무 DB 전용이다';
    END IF;
END
$$;

DROP TABLE IF EXISTS yeoljeong_business_registration_documents;

COMMIT;
